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

Phase 9b adds a second append-only ledger beside stage transitions.  A
``coverage_mission_stage_claim`` binds every automation-created formal Claim
and its supporting Evidence to the exact mission version and the company's
current playbook stage.  It does not pass a gate or create a Claim; the SEC
lane records the pair only after the policy-authorized formal write exists.
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
_ACCESSION_RE = re.compile(r"^[0-9]{10}-[0-9]{2}-[0-9]{6}$")

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
    # P9c: derived forecast-vs-actual outcome records.  Appended to the
    # vocabulary; existing missions that do not list it stay read-only here.
    "forecast_reconciliation",
    # P9d-1: search-driven source discovery records (which documents a
    # connected library holds for a covered company) and the budgeted
    # acquisition of the documents they name.  Never Evidence or Claims.
    "source_discovery",
)
DISCOVERY_DISPATCH_STATUSES: tuple[str, ...] = ("launched", "succeeded", "failed", "rejected")
DISCOVERED_DOCUMENT_STATUSES: tuple[str, ...] = (
    "discovered", "already_in_authority", "acquisition_launched", "acquired",
    "acquisition_failed",
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
_STAGE_CLAIM_FIELDS = frozenset({
    "schema_version", "id", "created_at", "mission_version_ref",
    "mission_version_hash", "company_ref", "stage_ref", "claim_version_ref",
    "claim_version_hash", "evidence_version_ref", "evidence_version_hash",
    "source_location", "actor_ref", "content_hash",
})
_SOURCE_DISCOVERY_FIELDS = frozenset({
    "schema_version", "id", "created_at", "mission_version_ref",
    "mission_version_hash", "company_ref", "source_ref", "discovery_plan_ref",
    "discovery_plan_hash", "spec_ref", "query_hash", "parameters",
    "connector_invocation_ref", "connector_invocation_hash",
    "source_envelope_ref", "source_envelope_hash", "document_refs",
    "new_document_refs", "in_authority_document_refs", "actor_ref",
    "requested_by", "content_hash",
})
_DISCOVERY_AUTHORIZATION_FIELDS = frozenset({
    "mission_version_ref", "mission_version_hash", "mission_ref", "company_ref",
    "ticker", "source_ref", "actor_ref", "requested_by", "scope",
    "max_alphaengine_calls_24h",
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


def validate_mission_stage_claim(value: Mapping[str, Any]) -> dict[str, Any]:
    wire = dict(value)
    if set(wire) != _STAGE_CLAIM_FIELDS or wire.get("schema_version") != SCHEMA_VERSION:
        raise CoverageMissionValidationError("mission stage claim has an invalid closed shape")
    for field in (
        "id", "created_at", "mission_version_ref", "company_ref", "claim_version_ref",
        "evidence_version_ref", "source_location",
    ):
        wire[field] = _text(wire[field], field)
    wire["mission_version_hash"] = _sha256(wire["mission_version_hash"], "mission_version_hash")
    wire["claim_version_hash"] = _sha256(wire["claim_version_hash"], "claim_version_hash")
    wire["evidence_version_hash"] = _sha256(
        wire["evidence_version_hash"], "evidence_version_hash"
    )
    wire["stage_ref"] = _vocabulary(wire["stage_ref"], STAGE_ORDER, "stage_ref")
    wire["actor_ref"] = _actor(wire["actor_ref"])
    if _AUTOMATION_RE.fullmatch(wire["actor_ref"]) is None:
        raise CoverageMissionValidationError(
            "mission stage claim actor_ref must use the automation: namespace"
        )
    if not wire["source_location"].startswith("sec:accession:"):
        raise CoverageMissionValidationError(
            "mission stage claim source_location must bind a SEC accession"
        )
    wire["content_hash"] = _sha256(wire["content_hash"], "content_hash")
    base = dict(wire)
    expected_hash = base.pop("content_hash")
    if content_hash(base) != expected_hash:
        raise CoverageMissionValidationError("mission stage claim content_hash is invalid")
    return wire


def validate_mission_source_discovery(value: Mapping[str, Any]) -> dict[str, Any]:
    """Validate one append-only source discovery record (P9d-1)."""

    wire = dict(value)
    if set(wire) != _SOURCE_DISCOVERY_FIELDS or wire.get("schema_version") != SCHEMA_VERSION:
        raise CoverageMissionValidationError("mission source discovery has an invalid closed shape")
    for field in (
        "id", "created_at", "mission_version_ref", "company_ref", "source_ref",
        "discovery_plan_ref", "spec_ref", "connector_invocation_ref",
        "source_envelope_ref",
    ):
        wire[field] = _text(wire[field], field)
    for field in (
        "mission_version_hash", "discovery_plan_hash", "query_hash",
        "connector_invocation_hash", "source_envelope_hash", "content_hash",
    ):
        wire[field] = _sha256(wire[field], field)
    if not isinstance(wire["parameters"], Mapping):
        raise CoverageMissionValidationError("mission source discovery parameters must be an object")
    wire["parameters"] = json.loads(canonical_json(wire["parameters"]))
    refs = _texts(wire["document_refs"], "document_refs")
    if len(set(refs)) != len(refs):
        raise CoverageMissionValidationError("mission source discovery document_refs must be unique")
    new_refs = _texts(wire["new_document_refs"], "new_document_refs")
    present = _texts(wire["in_authority_document_refs"], "in_authority_document_refs")
    if sorted(new_refs + present) != sorted(refs) or set(new_refs) & set(present):
        raise CoverageMissionValidationError(
            "mission source discovery must partition document_refs into new and in-authority"
        )
    wire["document_refs"] = refs
    wire["new_document_refs"] = new_refs
    wire["in_authority_document_refs"] = present
    wire["actor_ref"] = _actor(wire["actor_ref"])
    if _AUTOMATION_RE.fullmatch(wire["actor_ref"]) is None:
        raise CoverageMissionValidationError(
            "mission source discovery actor_ref must use the automation: namespace"
        )
    wire["requested_by"] = _actor(wire["requested_by"])
    base = dict(wire)
    expected_hash = base.pop("content_hash")
    if content_hash(base) != expected_hash:
        raise CoverageMissionValidationError("mission source discovery content_hash is invalid")
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

    def authorize_sec_lane(
        self,
        *,
        company_ref: str,
        ticker: str,
        actor_ref: str,
        mission_version_ref: str | None = None,
        mission_version_hash: str | None = None,
    ) -> dict[str, Any]:
        """Resolve and validate the mission grant for one zero-cost SEC run.

        The connector's own rate policy still bounds public HTTP calls.  SEC
        carries no paid-call or dollar charge, so this authorization reserves
        zero against the mission's paid budgets while retaining their exact
        limits in the returned receipt.
        """

        company_ref = _text(company_ref, "company_ref")
        ticker = _text(ticker, "ticker")
        actor_ref = _actor(actor_ref)
        if mission_version_ref is None:
            candidates: list[dict[str, Any]] = []
            for row in self.connection.execute(
                "SELECT mission_version_id FROM coverage_mission_pointer ORDER BY mission_ref"
            ).fetchall():
                mission = self.mission(row["mission_version_id"])
                if company_ref in {member["company_ref"] for member in mission["universe"]}:
                    candidates.append(mission)
            if len(candidates) != 1:
                raise CoverageMissionConflict(
                    "SEC automation requires exactly one active mission for the company"
                )
            mission = candidates[0]
        else:
            mission = self.mission(_text(mission_version_ref, "mission_version_ref"))
            pointer = self.connection.execute(
                "SELECT mission_version_id FROM coverage_mission_pointer WHERE mission_ref=?",
                (mission["mission_ref"],),
            ).fetchone()
            if pointer is None or pointer["mission_version_id"] != mission["id"]:
                raise CoverageMissionConflict("SEC automation must bind the active mission version")
        if mission_version_hash is not None and mission["content_hash"] != _sha256(
            mission_version_hash, "mission_version_hash"
        ):
            raise CoverageMissionConflict("SEC automation mission hash binding failed")
        if actor_ref != mission["autonomy"]["automation_principal"]:
            raise CoverageMissionConflict("SEC automation actor is not the mission principal")
        member = next(
            (item for item in mission["universe"] if item["company_ref"] == company_ref), None
        )
        if member is None or member["ticker"] != ticker:
            raise CoverageMissionConflict("SEC automation company/ticker is outside the mission universe")
        source = next(
            (item for item in mission["source_plan"] if item["source_ref"] == "source:sec-edgar"),
            None,
        )
        if source is None or source["status"] != "connected":
            raise CoverageMissionConflict("mission does not mark SEC EDGAR as connected")
        required_writes = {"claim", "evidence", "research_question", "observation", "stage_record"}
        missing = required_writes - set(mission["autonomy"]["may_write"])
        if missing:
            raise CoverageMissionConflict(
                f"mission does not grant SEC automation writes {sorted(missing)}"
            )
        cur = self.connection.cursor()
        self._validate_playbook_binding(cur, mission["bindings"]["playbook_version"])
        self._validate_constitution_binding(
            cur, mission["bindings"]["constitution_version"], mission["industry_ref"]
        )
        self._validate_mandate_binding(
            cur, mission["bindings"]["mandate_version"], mission["industry_ref"]
        )
        return {
            "mission_version_ref": mission["id"],
            "mission_version_hash": mission["content_hash"],
            "mission_ref": mission["mission_ref"],
            "company_ref": company_ref,
            "ticker": ticker,
            "actor_ref": actor_ref,
            "paid_calls_reserved": 0,
            "cost_usd_reserved": 0.0,
            "budget": dict(mission["budget"]),
        }

    def authorize_forecast_reconciliation(
        self,
        *,
        company_ref: str,
        actor_ref: str | None = None,
        mission_version_ref: str | None = None,
        mission_version_hash: str | None = None,
    ) -> dict[str, Any]:
        """Resolve the mission grant for automation forecast reconciliation.

        Requires exactly one active mission covering the company, the
        ``forecast_reconciliation`` write scope and the ``forecast_overturn``
        human checkpoint (so an overturn candidate has somewhere to escalate).
        Reconciliation is a zero-cost derived write; no budget is reserved.
        """

        company_ref = _text(company_ref, "company_ref")
        if mission_version_ref is None:
            candidates: list[dict[str, Any]] = []
            for row in self.connection.execute(
                "SELECT mission_version_id FROM coverage_mission_pointer ORDER BY mission_ref"
            ).fetchall():
                mission = self.mission(row["mission_version_id"])
                if company_ref in {member["company_ref"] for member in mission["universe"]}:
                    candidates.append(mission)
            if len(candidates) != 1:
                raise CoverageMissionConflict(
                    "forecast reconciliation requires exactly one active mission for the company"
                )
            mission = candidates[0]
        else:
            mission = self.mission(_text(mission_version_ref, "mission_version_ref"))
            pointer = self.connection.execute(
                "SELECT mission_version_id FROM coverage_mission_pointer WHERE mission_ref=?",
                (mission["mission_ref"],),
            ).fetchone()
            if pointer is None or pointer["mission_version_id"] != mission["id"]:
                raise CoverageMissionConflict(
                    "forecast reconciliation must bind the active mission version"
                )
        if mission_version_hash is not None and mission["content_hash"] != _sha256(
            mission_version_hash, "mission_version_hash"
        ):
            raise CoverageMissionConflict("forecast reconciliation mission hash binding failed")
        principal = mission["autonomy"]["automation_principal"]
        if actor_ref is None:
            actor_ref = principal
        elif _actor(actor_ref) != principal:
            raise CoverageMissionConflict(
                "forecast reconciliation actor is not the mission principal"
            )
        if company_ref not in {member["company_ref"] for member in mission["universe"]}:
            raise CoverageMissionConflict("company is outside the mission universe")
        if "forecast_reconciliation" not in mission["autonomy"]["may_write"]:
            raise CoverageMissionConflict(
                "mission does not grant forecast_reconciliation writes to automation"
            )
        if "forecast_overturn" not in mission["autonomy"]["human_checkpoints"]:
            raise CoverageMissionConflict(
                "mission does not list the forecast_overturn human checkpoint"
            )
        cur = self.connection.cursor()
        self._validate_playbook_binding(cur, mission["bindings"]["playbook_version"])
        self._validate_constitution_binding(
            cur, mission["bindings"]["constitution_version"], mission["industry_ref"]
        )
        self._validate_mandate_binding(
            cur, mission["bindings"]["mandate_version"], mission["industry_ref"]
        )
        return {
            "mission_version_ref": mission["id"],
            "mission_version_hash": mission["content_hash"],
            "mission_ref": mission["mission_ref"],
            "company_ref": company_ref,
            "actor_ref": actor_ref,
            "scope": "forecast_reconciliation",
        }

    # -- P9d-1: source discovery ---------------------------------------------

    def _resolve_active_mission_for_company(
        self,
        company_ref: str,
        *,
        purpose: str,
        mission_version_ref: str | None,
        mission_version_hash: str | None,
    ) -> dict[str, Any]:
        if mission_version_ref is None:
            candidates: list[dict[str, Any]] = []
            for row in self.connection.execute(
                "SELECT mission_version_id FROM coverage_mission_pointer ORDER BY mission_ref"
            ).fetchall():
                mission = self.mission(row["mission_version_id"])
                if company_ref in {member["company_ref"] for member in mission["universe"]}:
                    candidates.append(mission)
            if len(candidates) != 1:
                raise CoverageMissionConflict(
                    f"{purpose} requires exactly one active mission for the company"
                )
            mission = candidates[0]
        else:
            mission = self.mission(_text(mission_version_ref, "mission_version_ref"))
            pointer = self.connection.execute(
                "SELECT mission_version_id FROM coverage_mission_pointer WHERE mission_ref=?",
                (mission["mission_ref"],),
            ).fetchone()
            if pointer is None or pointer["mission_version_id"] != mission["id"]:
                raise CoverageMissionConflict(f"{purpose} must bind the active mission version")
        if mission_version_hash is not None and mission["content_hash"] != _sha256(
            mission_version_hash, "mission_version_hash"
        ):
            raise CoverageMissionConflict(f"{purpose} mission hash binding failed")
        return mission

    def authorize_source_discovery(
        self,
        *,
        company_ref: str,
        source_ref: str,
        requested_by: str,
        mission_version_ref: str | None = None,
        mission_version_hash: str | None = None,
    ) -> dict[str, Any]:
        """Resolve the mission grant for one budgeted library search.

        Automation (``requested_by`` equal to the mission principal) needs the
        source marked ``connected`` in the mission's source plan and the
        ``source_discovery`` + ``observation`` write scopes.  A ``human:``
        requester may run a discovery under a ``probe_only`` source (that is
        how an owner rehearses a connector before promoting it), but the
        record still binds the mission, its principal and its budget.
        """

        company_ref = _text(company_ref, "company_ref")
        source_ref = _text(source_ref, "source_ref")
        requested_by = _actor(requested_by, "requested_by")
        mission = self._resolve_active_mission_for_company(
            company_ref,
            purpose="source discovery",
            mission_version_ref=mission_version_ref,
            mission_version_hash=mission_version_hash,
        )
        principal = mission["autonomy"]["automation_principal"]
        human_request = _HUMAN_RE.fullmatch(requested_by) is not None
        if not human_request and requested_by != principal:
            raise CoverageMissionConflict("source discovery requester is not the mission principal")
        member = next(
            (item for item in mission["universe"] if item["company_ref"] == company_ref), None
        )
        if member is None:
            raise CoverageMissionConflict("company is outside the mission universe")
        source = next(
            (item for item in mission["source_plan"] if item["source_ref"] == source_ref), None
        )
        if source is None:
            raise CoverageMissionConflict(f"mission source plan does not list {source_ref}")
        if source["status"] == "not_connected":
            raise CoverageMissionConflict(f"mission marks {source_ref} as not_connected")
        if not human_request:
            if source["status"] != "connected":
                raise CoverageMissionConflict(
                    f"mission marks {source_ref} as {source['status']}; automation discovery "
                    "requires connected"
                )
            missing = {"source_discovery", "observation"} - set(mission["autonomy"]["may_write"])
            if missing:
                raise CoverageMissionConflict(
                    f"mission does not grant source discovery writes {sorted(missing)}"
                )
        cur = self.connection.cursor()
        self._validate_playbook_binding(cur, mission["bindings"]["playbook_version"])
        self._validate_constitution_binding(
            cur, mission["bindings"]["constitution_version"], mission["industry_ref"]
        )
        self._validate_mandate_binding(
            cur, mission["bindings"]["mandate_version"], mission["industry_ref"]
        )
        return {
            "mission_version_ref": mission["id"],
            "mission_version_hash": mission["content_hash"],
            "mission_ref": mission["mission_ref"],
            "company_ref": company_ref,
            "ticker": member["ticker"],
            "source_ref": source_ref,
            "actor_ref": principal,
            "requested_by": requested_by,
            "scope": "source_discovery",
            "max_alphaengine_calls_24h": int(mission["budget"]["max_alphaengine_calls_24h"]),
        }

    @staticmethod
    def _validate_discovery_authorization(value: Any) -> dict[str, Any]:
        if not isinstance(value, Mapping) or set(value) != _DISCOVERY_AUTHORIZATION_FIELDS:
            raise CoverageMissionValidationError("discovery authorization has an invalid closed shape")
        return json.loads(canonical_json(value))

    def record_discovery_dispatch(
        self,
        *,
        authorization: Mapping[str, Any],
        discovery_plan_ref: str,
        discovery_plan_hash: str,
        spec_ref: str,
        query_hash: str,
        ticket_ref: str,
    ) -> dict[str, Any]:
        """Record one launched discovery child (status ``launched``)."""

        authorization = self._validate_discovery_authorization(authorization)
        exact = self.authorize_source_discovery(
            company_ref=authorization["company_ref"],
            source_ref=authorization["source_ref"],
            requested_by=authorization["requested_by"],
            mission_version_ref=authorization["mission_version_ref"],
            mission_version_hash=authorization["mission_version_hash"],
        )
        if exact != authorization:
            raise CoverageMissionConflict("discovery authorization drifted")
        discovery_plan_ref = _text(discovery_plan_ref, "discovery_plan_ref")
        discovery_plan_hash = _sha256(discovery_plan_hash, "discovery_plan_hash")
        spec_ref = _text(spec_ref, "spec_ref")
        query_hash = _sha256(query_hash, "query_hash")
        ticket_ref = _text(ticket_ref, "ticket_ref")
        created_at = _now()
        identity = {
            "mission_version_ref": exact["mission_version_ref"],
            "company_ref": exact["company_ref"],
            "source_ref": exact["source_ref"],
            "spec_ref": spec_ref,
            "query_hash": query_hash,
            "ticket_ref": ticket_ref,
        }
        dispatch_id = _ref("mission-discovery-dispatch", identity)
        record = {
            **identity,
            "dispatch_id": dispatch_id,
            "mission_version_hash": exact["mission_version_hash"],
            "discovery_plan_ref": discovery_plan_ref,
            "discovery_plan_hash": discovery_plan_hash,
            "actor_ref": exact["actor_ref"],
            "requested_by": exact["requested_by"],
            "authorization": exact,
            "status": "launched",
            "failure_reason": None,
            "created_at": created_at,
            "updated_at": created_at,
        }
        with self._transaction() as cur:
            existing = cur.execute(
                "SELECT dispatch_id FROM coverage_mission_discovery_dispatches WHERE dispatch_id=?",
                (dispatch_id,),
            ).fetchone()
            if existing is not None:
                raise CoverageMissionConflict("discovery dispatch already recorded for this ticket")
            cur.execute(
                "INSERT INTO coverage_mission_discovery_dispatches"
                "(dispatch_id,mission_version_ref,mission_version_hash,company_ref,source_ref,"
                "discovery_plan_ref,discovery_plan_hash,spec_ref,query_hash,actor_ref,requested_by,"
                "authorization_json,status,ticket_ref,failure_reason,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    dispatch_id, exact["mission_version_ref"], exact["mission_version_hash"],
                    exact["company_ref"], exact["source_ref"], discovery_plan_ref,
                    discovery_plan_hash, spec_ref, query_hash, exact["actor_ref"],
                    exact["requested_by"], canonical_json(exact), "launched", ticket_ref,
                    None, created_at, created_at,
                ),
            )
        return record

    def _dispatch_row(self, row: Any) -> dict[str, Any]:
        return {
            "dispatch_id": row["dispatch_id"],
            "mission_version_ref": row["mission_version_ref"],
            "mission_version_hash": row["mission_version_hash"],
            "company_ref": row["company_ref"],
            "source_ref": row["source_ref"],
            "discovery_plan_ref": row["discovery_plan_ref"],
            "discovery_plan_hash": row["discovery_plan_hash"],
            "spec_ref": row["spec_ref"],
            "query_hash": row["query_hash"],
            "actor_ref": row["actor_ref"],
            "requested_by": row["requested_by"],
            "authorization": json.loads(row["authorization_json"]),
            "status": row["status"],
            "ticket_ref": row["ticket_ref"],
            "failure_reason": row["failure_reason"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def open_discovery_dispatches(self, *, limit: int = 20) -> list[dict[str, Any]]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise CoverageMissionValidationError("discovery dispatch limit must be 1..100")
        rows = self.connection.execute(
            "SELECT * FROM coverage_mission_discovery_dispatches WHERE status='launched' "
            "ORDER BY created_at,dispatch_id LIMIT ?", (limit,),
        ).fetchall()
        return [self._dispatch_row(row) for row in rows]

    def discovery_dispatches(
        self, mission_version_ref: str, *, company_ref: str | None = None,
        spec_ref: str | None = None, limit: int = 100,
    ) -> list[dict[str, Any]]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise CoverageMissionValidationError("discovery dispatch limit must be 1..1000")
        query = "SELECT * FROM coverage_mission_discovery_dispatches WHERE mission_version_ref=?"
        params: list[Any] = [_text(mission_version_ref, "mission_version_ref")]
        if company_ref is not None:
            query += " AND company_ref=?"
            params.append(_text(company_ref, "company_ref"))
        if spec_ref is not None:
            query += " AND spec_ref=?"
            params.append(_text(spec_ref, "spec_ref"))
        query += " ORDER BY created_at DESC,dispatch_id DESC LIMIT ?"
        params.append(limit)
        return [self._dispatch_row(row) for row in self.connection.execute(query, params).fetchall()]

    def settle_discovery_dispatch(
        self, dispatch_id: str, *, status: str, reason: str | None = None
    ) -> dict[str, Any]:
        dispatch_id = _text(dispatch_id, "dispatch_id")
        status = _vocabulary(status, ("succeeded", "failed", "rejected"), "status")
        if status != "succeeded":
            reason = _text(reason, "reason")
        elif reason is not None:
            raise CoverageMissionValidationError("a succeeded dispatch carries no failure reason")
        with self._transaction() as cur:
            row = cur.execute(
                "SELECT * FROM coverage_mission_discovery_dispatches WHERE dispatch_id=?",
                (dispatch_id,),
            ).fetchone()
            if row is None:
                raise CoverageMissionNotFound("discovery dispatch was not found")
            if row["status"] != "launched":
                if row["status"] == status and row["failure_reason"] == reason:
                    return self._dispatch_row(row)
                raise CoverageMissionConflict("discovery dispatch is already settled differently")
            now = _now()
            cur.execute(
                "UPDATE coverage_mission_discovery_dispatches SET status=?,failure_reason=?,"
                "updated_at=? WHERE dispatch_id=? AND status='launched'",
                (status, reason, now, dispatch_id),
            )
            if cur.rowcount != 1:
                raise CoverageMissionConflict("discovery dispatch state changed concurrently")
            row = cur.execute(
                "SELECT * FROM coverage_mission_discovery_dispatches WHERE dispatch_id=?",
                (dispatch_id,),
            ).fetchone()
        return self._dispatch_row(row)

    def record_source_discovery(
        self,
        *,
        authorization: Mapping[str, Any],
        discovery_plan_ref: str,
        discovery_plan_hash: str,
        spec_ref: str,
        query_hash: str,
        parameters: Mapping[str, Any],
        connector_invocation_ref: str,
        connector_invocation_hash: str,
        source_envelope_ref: str,
        source_envelope_hash: str,
        document_refs: list[str],
        in_authority_document_refs: list[str],
    ) -> dict[str, Any]:
        """Append one discovery record and register its new documents.

        The search itself already left Core connector authority behind; this
        binds that exact invocation / envelope to the mission, company and
        plan spec, and opens one ``discovered`` document row per ref Core does
        not yet hold.  Documents already in authority are recorded as such
        and never re-queued.
        """

        authorization = self._validate_discovery_authorization(authorization)
        exact = self.authorize_source_discovery(
            company_ref=authorization["company_ref"],
            source_ref=authorization["source_ref"],
            requested_by=authorization["requested_by"],
            mission_version_ref=authorization["mission_version_ref"],
            mission_version_hash=authorization["mission_version_hash"],
        )
        if exact != authorization:
            raise CoverageMissionConflict("discovery authorization drifted")
        discovery_plan_ref = _text(discovery_plan_ref, "discovery_plan_ref")
        discovery_plan_hash = _sha256(discovery_plan_hash, "discovery_plan_hash")
        spec_ref = _text(spec_ref, "spec_ref")
        query_hash = _sha256(query_hash, "query_hash")
        connector_invocation_ref = _text(connector_invocation_ref, "connector_invocation_ref")
        connector_invocation_hash = _sha256(connector_invocation_hash, "connector_invocation_hash")
        source_envelope_ref = _text(source_envelope_ref, "source_envelope_ref")
        source_envelope_hash = _sha256(source_envelope_hash, "source_envelope_hash")
        refs = _texts(document_refs, "document_refs")
        present = _texts(in_authority_document_refs, "in_authority_document_refs")
        if not set(present) <= set(refs):
            raise CoverageMissionValidationError(
                "in_authority_document_refs must be a subset of document_refs"
            )
        new_refs = [ref for ref in refs if ref not in set(present)]
        invocation = self.connection.execute(
            "SELECT content_hash FROM connector_invocations WHERE connector_invocation_id=?",
            (connector_invocation_ref,),
        ).fetchone()
        if invocation is None or invocation["content_hash"] != connector_invocation_hash:
            raise CoverageMissionConflict("discovery connector invocation binding failed")
        envelope = self.connection.execute(
            "SELECT connector_invocation_ref,content_hash,record_json FROM "
            "connector_source_envelopes WHERE source_envelope_id=?",
            (source_envelope_ref,),
        ).fetchone()
        if (
            envelope is None
            or envelope["content_hash"] != source_envelope_hash
            or envelope["connector_invocation_ref"] != connector_invocation_ref
        ):
            raise CoverageMissionConflict("discovery source envelope binding failed")
        envelope_record = json.loads(envelope["record_json"])
        if (
            envelope_record.get("source") != exact["source_ref"]
            or envelope_record.get("operation") != "search_library"
            or list(envelope_record.get("source_record_refs") or []) != refs
        ):
            raise CoverageMissionConflict("discovery document_refs differ from the source envelope")
        identity = {
            "mission_version_ref": exact["mission_version_ref"],
            "mission_version_hash": exact["mission_version_hash"],
            "company_ref": exact["company_ref"],
            "source_ref": exact["source_ref"],
            "discovery_plan_ref": discovery_plan_ref,
            "discovery_plan_hash": discovery_plan_hash,
            "spec_ref": spec_ref,
            "query_hash": query_hash,
            "parameters": json.loads(canonical_json(parameters)),
            "connector_invocation_ref": connector_invocation_ref,
            "connector_invocation_hash": connector_invocation_hash,
            "source_envelope_ref": source_envelope_ref,
            "source_envelope_hash": source_envelope_hash,
            "document_refs": refs,
            "new_document_refs": new_refs,
            "in_authority_document_refs": present,
            "actor_ref": exact["actor_ref"],
            "requested_by": exact["requested_by"],
        }
        record_id = _ref("mission-source-discovery", identity)
        existing = self.connection.execute(
            "SELECT record_json FROM coverage_mission_source_discoveries "
            "WHERE mission_version_ref=? AND source_envelope_ref=?",
            (exact["mission_version_ref"], source_envelope_ref),
        ).fetchone()
        if existing is not None:
            wire = validate_mission_source_discovery(
                _canonical_record(existing["record_json"], "mission source discovery")
            )
            if wire["id"] != record_id:
                raise CoverageMissionConflict("source envelope is already bound to another discovery")
            return {**wire, "status": "duplicate"}
        created_at = _now()
        record = {"schema_version": SCHEMA_VERSION, "id": record_id, "created_at": created_at, **identity}
        wire = {**record, "content_hash": content_hash(record)}
        validate_mission_source_discovery(wire)
        with self._transaction() as cur:
            cur.execute(
                "INSERT INTO coverage_mission_source_discoveries"
                "(record_id,mission_version_ref,mission_version_hash,company_ref,source_ref,"
                "discovery_plan_ref,discovery_plan_hash,spec_ref,query_hash,connector_invocation_ref,"
                "source_envelope_ref,source_envelope_hash,actor_ref,requested_by,record_json,"
                "content_hash,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    record_id, exact["mission_version_ref"], exact["mission_version_hash"],
                    exact["company_ref"], exact["source_ref"], discovery_plan_ref,
                    discovery_plan_hash, spec_ref, query_hash, connector_invocation_ref,
                    source_envelope_ref, source_envelope_hash, exact["actor_ref"],
                    exact["requested_by"], canonical_json(wire), wire["content_hash"], created_at,
                ),
            )
            for ref in refs:
                status = "already_in_authority" if ref in set(present) else "discovered"
                document_id = _ref(
                    "mission-discovered-document",
                    {"mission_version_ref": exact["mission_version_ref"], "document_ref": ref},
                )
                if cur.execute(
                    "SELECT 1 FROM coverage_mission_discovered_documents WHERE record_id=?",
                    (document_id,),
                ).fetchone() is not None:
                    continue
                cur.execute(
                    "INSERT INTO coverage_mission_discovered_documents"
                    "(record_id,mission_version_ref,company_ref,source_ref,document_ref,"
                    "discovery_ref,status,ticket_ref,failure_reason,created_at,updated_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        document_id, exact["mission_version_ref"], exact["company_ref"],
                        exact["source_ref"], ref, record_id, status, None, None,
                        created_at, created_at,
                    ),
                )
        return {**wire, "status": "fresh"}

    def source_discoveries(
        self, mission_version_ref: str, *, company_ref: str | None = None,
        spec_ref: str | None = None, limit: int = 100,
    ) -> list[dict[str, Any]]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise CoverageMissionValidationError("discovery limit must be 1..1000")
        query = "SELECT * FROM coverage_mission_source_discoveries WHERE mission_version_ref=?"
        params: list[Any] = [_text(mission_version_ref, "mission_version_ref")]
        if company_ref is not None:
            query += " AND company_ref=?"
            params.append(_text(company_ref, "company_ref"))
        if spec_ref is not None:
            query += " AND spec_ref=?"
            params.append(_text(spec_ref, "spec_ref"))
        query += " ORDER BY created_at DESC,record_id DESC LIMIT ?"
        params.append(limit)
        records = []
        for row in self.connection.execute(query, params).fetchall():
            wire = validate_mission_source_discovery(
                _canonical_record(row["record_json"], "mission source discovery")
            )
            if wire["id"] != row["record_id"] or wire["content_hash"] != row["content_hash"]:
                raise CoverageMissionConflict("mission source discovery authority drifted")
            records.append(wire)
        return records

    def _document_row(self, row: Any) -> dict[str, Any]:
        return {
            "record_id": row["record_id"],
            "mission_version_ref": row["mission_version_ref"],
            "company_ref": row["company_ref"],
            "source_ref": row["source_ref"],
            "document_ref": row["document_ref"],
            "discovery_ref": row["discovery_ref"],
            "status": row["status"],
            "ticket_ref": row["ticket_ref"],
            "failure_reason": row["failure_reason"],
            "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    def discovered_documents(
        self, mission_version_ref: str, *, company_ref: str | None = None,
        status: str | None = None, limit: int = 100,
    ) -> list[dict[str, Any]]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise CoverageMissionValidationError("discovered document limit must be 1..1000")
        query = "SELECT * FROM coverage_mission_discovered_documents WHERE mission_version_ref=?"
        params: list[Any] = [_text(mission_version_ref, "mission_version_ref")]
        if company_ref is not None:
            query += " AND company_ref=?"
            params.append(_text(company_ref, "company_ref"))
        if status is not None:
            query += " AND status=?"
            params.append(_vocabulary(status, DISCOVERED_DOCUMENT_STATUSES, "status"))
        query += " ORDER BY created_at,record_id LIMIT ?"
        params.append(limit)
        return [self._document_row(row) for row in self.connection.execute(query, params).fetchall()]

    def next_discovered_document(self) -> dict[str, Any] | None:
        """Oldest ``discovered`` document across active missions, or None."""

        row = self.connection.execute(
            "SELECT d.* FROM coverage_mission_discovered_documents d "
            "JOIN coverage_mission_pointer p ON p.mission_version_id=d.mission_version_ref "
            "WHERE d.status='discovered' ORDER BY d.created_at,d.record_id LIMIT 1"
        ).fetchone()
        return None if row is None else self._document_row(row)

    def launched_discovered_documents(self, *, limit: int = 20) -> list[dict[str, Any]]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 100:
            raise CoverageMissionValidationError("discovered document limit must be 1..100")
        rows = self.connection.execute(
            "SELECT * FROM coverage_mission_discovered_documents WHERE status='acquisition_launched' "
            "ORDER BY updated_at,record_id LIMIT ?", (limit,),
        ).fetchall()
        return [self._document_row(row) for row in rows]

    def mark_discovered_document_launched(self, record_id: str, ticket_ref: str) -> dict[str, Any]:
        record_id = _text(record_id, "record_id")
        ticket_ref = _text(ticket_ref, "ticket_ref")
        with self._transaction() as cur:
            row = cur.execute(
                "SELECT * FROM coverage_mission_discovered_documents WHERE record_id=?", (record_id,)
            ).fetchone()
            if row is None:
                raise CoverageMissionNotFound("discovered document was not found")
            if row["status"] != "discovered":
                if row["status"] == "acquisition_launched" and row["ticket_ref"] == ticket_ref:
                    return self._document_row(row)
                raise CoverageMissionConflict("discovered document is not awaiting acquisition")
            now = _now()
            cur.execute(
                "UPDATE coverage_mission_discovered_documents SET status='acquisition_launched',"
                "ticket_ref=?,updated_at=? WHERE record_id=? AND status='discovered'",
                (ticket_ref, now, record_id),
            )
            if cur.rowcount != 1:
                raise CoverageMissionConflict("discovered document state changed concurrently")
            row = cur.execute(
                "SELECT * FROM coverage_mission_discovered_documents WHERE record_id=?", (record_id,)
            ).fetchone()
        return self._document_row(row)

    def settle_discovered_document(
        self, record_id: str, *, status: str, reason: str | None = None
    ) -> dict[str, Any]:
        record_id = _text(record_id, "record_id")
        status = _vocabulary(status, ("acquired", "acquisition_failed"), "status")
        if status == "acquisition_failed":
            reason = _text(reason, "reason")
        elif reason is not None:
            raise CoverageMissionValidationError("an acquired document carries no failure reason")
        with self._transaction() as cur:
            row = cur.execute(
                "SELECT * FROM coverage_mission_discovered_documents WHERE record_id=?", (record_id,)
            ).fetchone()
            if row is None:
                raise CoverageMissionNotFound("discovered document was not found")
            if row["status"] != "acquisition_launched":
                if row["status"] == status and row["failure_reason"] == reason:
                    return self._document_row(row)
                raise CoverageMissionConflict("discovered document is not in a launched acquisition")
            now = _now()
            cur.execute(
                "UPDATE coverage_mission_discovered_documents SET status=?,failure_reason=?,"
                "updated_at=? WHERE record_id=? AND status='acquisition_launched'",
                (status, reason, now, record_id),
            )
            if cur.rowcount != 1:
                raise CoverageMissionConflict("discovered document state changed concurrently")
            row = cur.execute(
                "SELECT * FROM coverage_mission_discovered_documents WHERE record_id=?", (record_id,)
            ).fetchone()
        return self._document_row(row)

    def sec_lane_authorization_for_company(self, company_ref: str) -> dict[str, Any]:
        """Resolve the sole active mission and return its exact SEC grant."""

        company_ref = _text(company_ref, "company_ref")
        matches: list[tuple[dict[str, Any], dict[str, Any]]] = []
        for row in self.connection.execute(
            "SELECT mission_version_id FROM coverage_mission_pointer ORDER BY mission_ref"
        ).fetchall():
            mission = self.mission(row["mission_version_id"])
            member = next(
                (item for item in mission["universe"] if item["company_ref"] == company_ref),
                None,
            )
            if member is not None:
                matches.append((mission, member))
        if len(matches) != 1:
            raise CoverageMissionConflict(
                "SEC observation requires exactly one active mission for the company"
            )
        mission, member = matches[0]
        return self.authorize_sec_lane(
            company_ref=company_ref,
            ticker=member["ticker"],
            actor_ref=mission["autonomy"]["automation_principal"],
            mission_version_ref=mission["id"],
            mission_version_hash=mission["content_hash"],
        )

    def queue_sec_dispatch(
        self,
        *,
        authorization: Mapping[str, Any],
        form: str,
        filed_from: str,
        filed_to: str,
        expected_accession: str,
        observation_ref: str,
    ) -> dict[str, Any]:
        """Persist an idempotent mission SEC dispatch before the launcher slot."""

        authorization = dict(authorization)
        exact = self.authorize_sec_lane(
            company_ref=authorization.get("company_ref"),
            ticker=authorization.get("ticker"),
            actor_ref=authorization.get("actor_ref"),
            mission_version_ref=authorization.get("mission_version_ref"),
            mission_version_hash=authorization.get("mission_version_hash"),
        )
        if canonical_json(exact) != canonical_json(authorization):
            raise CoverageMissionConflict("SEC dispatch authorization drifted")
        if form not in {"10-Q", "10-K"}:
            raise CoverageMissionValidationError("SEC dispatch form must be 10-Q or 10-K")
        filed_from = _text(filed_from, "filed_from")
        filed_to = _text(filed_to, "filed_to")
        if filed_from > filed_to:
            raise CoverageMissionValidationError("SEC dispatch filing window is reversed")
        expected_accession = _text(expected_accession, "expected_accession")
        if _ACCESSION_RE.fullmatch(expected_accession) is None:
            raise CoverageMissionValidationError("SEC dispatch accession is invalid")
        observation_ref = _text(observation_ref, "observation_ref")
        request = {
            "mission_version_ref": exact["mission_version_ref"],
            "mission_version_hash": exact["mission_version_hash"],
            "company_ref": exact["company_ref"],
            "ticker": exact["ticker"],
            "actor_ref": exact["actor_ref"],
            "form": form,
            "filed_from": filed_from,
            "filed_to": filed_to,
            "expected_accession": expected_accession,
            "observation_ref": observation_ref,
        }
        request_hash = content_hash(request)
        dispatch_id = f"mission-sec-dispatch:{request_hash[:32]}"
        existing = self.connection.execute(
            "SELECT * FROM coverage_mission_sec_dispatches WHERE dispatch_id=?",
            (dispatch_id,),
        ).fetchone()
        if existing is not None:
            if existing["request_hash"] != request_hash:
                raise CoverageMissionConflict("SEC dispatch identity drifted")
            return {**dict(existing), "authorization": json.loads(existing["authorization_json"]),
                    "status_marker": "duplicate"}
        now = _now()
        with self._transaction() as cur:
            cur.execute(
                "INSERT INTO coverage_mission_sec_dispatches"
                "(dispatch_id,mission_version_ref,mission_version_hash,company_ref,ticker,actor_ref,"
                "form,filed_from,filed_to,expected_accession,observation_ref,authorization_json,"
                "request_hash,status,ticket_ref,failure_reason,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,'pending',NULL,NULL,?,?)",
                (
                    dispatch_id, exact["mission_version_ref"], exact["mission_version_hash"],
                    exact["company_ref"], exact["ticker"], exact["actor_ref"], form,
                    filed_from, filed_to, expected_accession, observation_ref,
                    canonical_json(exact), request_hash, now, now,
                ),
            )
        return {
            **request, "dispatch_id": dispatch_id, "authorization": exact,
            "request_hash": request_hash, "status": "pending", "ticket_ref": None,
            "created_at": now, "updated_at": now, "status_marker": "fresh",
        }

    def pending_sec_dispatches(self, *, limit: int = 1) -> list[dict[str, Any]]:
        if type(limit) is not int or not 1 <= limit <= 20:
            raise CoverageMissionValidationError("SEC dispatch limit must be 1..20")
        rows = self.connection.execute(
            "SELECT * FROM coverage_mission_sec_dispatches WHERE status='pending' "
            "ORDER BY created_at,dispatch_id LIMIT ?", (limit,),
        ).fetchall()
        return [
            {**dict(row), "authorization": json.loads(row["authorization_json"])}
            for row in rows
        ]

    def mark_sec_dispatch_launched(self, dispatch_id: str, ticket_ref: str) -> dict[str, Any]:
        dispatch_id = _text(dispatch_id, "dispatch_id")
        ticket_ref = _text(ticket_ref, "ticket_ref")
        row = self.connection.execute(
            "SELECT * FROM coverage_mission_sec_dispatches WHERE dispatch_id=?", (dispatch_id,)
        ).fetchone()
        if row is None:
            raise CoverageMissionNotFound("mission SEC dispatch was not found")
        if row["status"] == "launched":
            if row["ticket_ref"] != ticket_ref:
                raise CoverageMissionConflict("mission SEC dispatch bound another ticket")
            return {**dict(row), "authorization": json.loads(row["authorization_json"]),
                    "status_marker": "duplicate"}
        now = _now()
        with self._transaction() as cur:
            cur.execute(
                "UPDATE coverage_mission_sec_dispatches SET status='launched',ticket_ref=?,"
                "updated_at=? WHERE dispatch_id=? AND status='pending'",
                (ticket_ref, now, dispatch_id),
            )
            if cur.rowcount != 1:
                raise CoverageMissionConflict("mission SEC dispatch state changed concurrently")
        return {
            **dict(row), "status": "launched", "ticket_ref": ticket_ref,
            "updated_at": now, "authorization": json.loads(row["authorization_json"]),
            "status_marker": "fresh",
        }

    def mark_sec_dispatch_rejected(self, dispatch_id: str, reason: str) -> dict[str, Any]:
        dispatch_id = _text(dispatch_id, "dispatch_id")
        reason = _text(reason, "reason")
        row = self.connection.execute(
            "SELECT * FROM coverage_mission_sec_dispatches WHERE dispatch_id=?", (dispatch_id,)
        ).fetchone()
        if row is None:
            raise CoverageMissionNotFound("mission SEC dispatch was not found")
        if row["status"] == "rejected":
            if row["failure_reason"] != reason:
                raise CoverageMissionConflict("mission SEC dispatch has another rejection reason")
            return {**dict(row), "authorization": json.loads(row["authorization_json"]),
                    "status_marker": "duplicate"}
        if row["status"] != "pending":
            raise CoverageMissionConflict("launched SEC dispatch cannot be rejected")
        now = _now()
        with self._transaction() as cur:
            cur.execute(
                "UPDATE coverage_mission_sec_dispatches SET status='rejected',failure_reason=?,"
                "updated_at=? WHERE dispatch_id=? AND status='pending'",
                (reason, now, dispatch_id),
            )
            if cur.rowcount != 1:
                raise CoverageMissionConflict("mission SEC dispatch state changed concurrently")
        return {
            **dict(row), "status": "rejected", "failure_reason": reason,
            "updated_at": now, "authorization": json.loads(row["authorization_json"]),
            "status_marker": "fresh",
        }

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

    def record_stage_claim(
        self,
        *,
        mission_version_ref: str,
        mission_version_hash: str,
        company_ref: str,
        ticker: str,
        claim_version_ref: str,
        claim_version_hash: str,
        evidence_version_ref: str,
        evidence_version_hash: str,
        source_location: str,
        actor_ref: str,
    ) -> dict[str, Any]:
        """Bind one already-formal Claim/Evidence pair to the current stage."""

        authorization = self.authorize_sec_lane(
            company_ref=company_ref,
            ticker=ticker,
            actor_ref=actor_ref,
            mission_version_ref=mission_version_ref,
            mission_version_hash=mission_version_hash,
        )
        claim_version_ref = _text(claim_version_ref, "claim_version_ref")
        evidence_version_ref = _text(evidence_version_ref, "evidence_version_ref")
        claim_version_hash = _sha256(claim_version_hash, "claim_version_hash")
        evidence_version_hash = _sha256(evidence_version_hash, "evidence_version_hash")
        source_location = _text(source_location, "source_location")
        claim = self.store.get_claim(claim_version_ref)
        if (
            claim is None
            or claim.get("content_hash") != claim_version_hash
            or (claim.get("claim") or {}).get("subject_ref") != company_ref
        ):
            raise CoverageMissionConflict("formal Claim binding failed")
        evidence = self.connection.execute(
            "SELECT content_hash FROM evidence_versions WHERE evidence_version_id=?",
            (evidence_version_ref,),
        ).fetchone()
        if evidence is None or evidence["content_hash"] != evidence_version_hash:
            raise CoverageMissionConflict("formal Evidence binding failed")

        progress = self.mission_progress(authorization["mission_ref"])
        company = next(
            item for item in progress["companies"] if item["company_ref"] == company_ref
        )
        stage_ref = company["next_stage"] or company["current_stage"] or STAGE_ORDER[0]
        if company["current_stage"] != stage_ref:
            self.record_stage(
                mission_version_ref=mission_version_ref,
                mission_version_hash=mission_version_hash,
                company_ref=company_ref,
                stage_ref=stage_ref,
                status="entered",
                evidence_refs=[],
                rationale="SEC automation entered the current mission stage before recording evidence.",
                actor_ref=actor_ref,
                idempotency_key=f"mission-stage-enter:{mission_version_ref}:{company_ref}:{stage_ref}",
            )
        identity = {
            "mission_version_ref": mission_version_ref,
            "mission_version_hash": mission_version_hash,
            "company_ref": company_ref,
            "stage_ref": stage_ref,
            "claim_version_ref": claim_version_ref,
            "claim_version_hash": claim_version_hash,
            "evidence_version_ref": evidence_version_ref,
            "evidence_version_hash": evidence_version_hash,
            "source_location": source_location,
            "actor_ref": actor_ref,
        }
        record_id = _ref("mission-stage-claim", identity)
        existing = self.connection.execute(
            "SELECT record_json FROM coverage_mission_stage_claims "
            "WHERE mission_version_ref=? AND claim_version_ref=?",
            (mission_version_ref, claim_version_ref),
        ).fetchone()
        if existing is not None:
            wire = validate_mission_stage_claim(
                _canonical_record(existing["record_json"], "mission stage claim")
            )
            if wire["id"] != record_id:
                raise CoverageMissionConflict("formal Claim is already bound differently")
            return {**wire, "status": "duplicate"}
        created_at = _now()
        record = {
            "schema_version": SCHEMA_VERSION,
            "id": record_id,
            "created_at": created_at,
            **identity,
        }
        wire = {**record, "content_hash": content_hash(record)}
        validate_mission_stage_claim(wire)
        with self._transaction() as cur:
            cur.execute(
                "INSERT INTO coverage_mission_stage_claims"
                "(record_id,mission_version_ref,company_ref,stage_ref,claim_version_ref,"
                "claim_version_hash,evidence_version_ref,evidence_version_hash,source_location,"
                "actor_ref,record_json,content_hash,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    record_id, mission_version_ref, company_ref, stage_ref, claim_version_ref,
                    claim_version_hash, evidence_version_ref, evidence_version_hash,
                    source_location, actor_ref, canonical_json(wire), wire["content_hash"], created_at,
                ),
            )
        return {**wire, "status": "fresh"}

    def stage_claims(
        self, mission_version_ref: str, company_ref: str | None = None
    ) -> list[dict[str, Any]]:
        mission_version_ref = _text(mission_version_ref, "mission_version_ref")
        query = "SELECT * FROM coverage_mission_stage_claims WHERE mission_version_ref=?"
        params: list[Any] = [mission_version_ref]
        if company_ref is not None:
            query += " AND company_ref=?"
            params.append(_text(company_ref, "company_ref"))
        query += " ORDER BY created_at,record_id"
        records = []
        for row in self.connection.execute(query, params).fetchall():
            wire = validate_mission_stage_claim(
                _canonical_record(row["record_json"], "mission stage claim")
            )
            if wire["id"] != row["record_id"] or wire["content_hash"] != row["content_hash"]:
                raise CoverageMissionConflict("mission stage claim authority drifted")
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
                "claim_count": self.connection.execute(
                    "SELECT COUNT(*) FROM coverage_mission_stage_claims "
                    "WHERE mission_version_ref=? AND company_ref=?",
                    (mission["id"], member["company_ref"]),
                ).fetchone()[0],
                "discovery_count": self.connection.execute(
                    "SELECT COUNT(*) FROM coverage_mission_source_discoveries "
                    "WHERE mission_version_ref=? AND company_ref=?",
                    (mission["id"], member["company_ref"]),
                ).fetchone()[0],
                "discovered_document_count": self.connection.execute(
                    "SELECT COUNT(*) FROM coverage_mission_discovered_documents "
                    "WHERE mission_version_ref=? AND company_ref=?",
                    (mission["id"], member["company_ref"]),
                ).fetchone()[0],
                "acquired_document_count": self.connection.execute(
                    "SELECT COUNT(*) FROM coverage_mission_discovered_documents "
                    "WHERE mission_version_ref=? AND company_ref=? "
                    "AND status IN ('acquired','already_in_authority')",
                    (mission["id"], member["company_ref"]),
                ).fetchone()[0],
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
    "DISCOVERED_DOCUMENT_STATUSES",
    "DISCOVERY_DISPATCH_STATUSES",
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
    "validate_mission_stage_claim",
    "validate_mission_source_discovery",
]
