"""Bounded, outcome-driven research planning for one exact question.

The planner in this module proposes only one of two closed actions: run one
human-admitted probe template, or request one closed terminal state.  Core
re-reads every authority binding, owns admission and budget checks, and uses
the existing Scheduler and Observability workflow authorities for execution.

This development slice deliberately does not call a model, a live connector,
or the Evidence/Claim writer.  ``ResearchOutcome`` is source-level state; even
a coverage-complete unobservable result remains a candidate rather than a
formal negative Claim.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .contracts import WorkOrder
from .research_question_backlog import read_exact_backlog_question_version
from .store import canonical_json, content_hash


SCHEMA_VERSION = "0.1"
CAPITAL_LEASE_PLANNER_REF = "planner:capital-lease-checklist:0.1"
CAPITAL_LEASE_PLANNER_HASH = content_hash({
    "planner_ref": CAPITAL_LEASE_PLANNER_REF,
    "algorithm": "directive_then_first_uncovered_else_closed_terminal",
    "proposal_contract": "planner-proposal-version:0.1",
})
_SCHEMA_PATH = Path(__file__).with_name("bounded_planner_loop_schema.sql")
_HUMAN_RE = re.compile(r"^human:[A-Za-z0-9][A-Za-z0-9._/@:-]*$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_CONTROL_EFFECTS = frozenset({
    "focus_coverage_item", "request_replan", "deprioritize",
})
_TERMINAL_REASONS = frozenset({
    "coverage_complete_unobservable_candidate",
    "evidence_observed_for_review",
    "human_replan_required",
    "human_deprioritized",
})
_OUTCOME_KINDS = frozenset({
    "observed", "not_found_in_scope", "source_unavailable",
})


class BoundedPlannerError(Exception):
    pass


class BoundedPlannerValidationError(BoundedPlannerError, ValueError):
    pass


class BoundedPlannerConflict(BoundedPlannerError):
    pass


class BoundedPlannerNotFound(BoundedPlannerError):
    pass


class BoundedPlannerPending(BoundedPlannerError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise BoundedPlannerValidationError(f"{name} must be a non-empty string")
    return value.strip()


def _sha256(value: Any, name: str) -> str:
    value = _text(value, name)
    if _SHA256_RE.fullmatch(value) is None:
        raise BoundedPlannerValidationError(f"{name} must be lowercase SHA-256")
    return value


def _positive_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise BoundedPlannerValidationError(f"{name} must be a positive integer")
    return value


def _closed(value: Any, fields: set[str], name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise BoundedPlannerValidationError(f"{name} must be an object")
    result = dict(value)
    if set(result) != fields:
        missing = sorted(fields - set(result))
        unknown = sorted(set(result) - fields)
        raise BoundedPlannerValidationError(
            f"{name} has invalid closed shape; missing={missing}, unknown={unknown}"
        )
    return result


def _unique_texts(value: Any, name: str, *, nonempty: bool = False) -> list[str]:
    if not isinstance(value, (list, tuple)):
        raise BoundedPlannerValidationError(f"{name} must be an array")
    result = [_text(item, f"{name}[]") for item in value]
    if nonempty and not result:
        raise BoundedPlannerValidationError(f"{name} must not be empty")
    if len(set(result)) != len(result):
        raise BoundedPlannerValidationError(f"{name} must contain unique values")
    return result


def _human(value: Any, name: str = "actor_ref") -> str:
    value = _text(value, name)
    if _HUMAN_RE.fullmatch(value) is None:
        raise BoundedPlannerValidationError(f"{name} must use the human: namespace")
    return value


def _record(base: Mapping[str, Any]) -> dict[str, Any]:
    wire = dict(base)
    wire["content_hash"] = content_hash(wire)
    return wire


def _deterministic_ref(prefix: str, identity: Mapping[str, Any]) -> str:
    return f"{prefix}:{content_hash(identity)[:32]}"


def _decode_record(row: sqlite3.Row | None, name: str) -> dict[str, Any]:
    if row is None:
        raise BoundedPlannerNotFound(name)
    try:
        wire = json.loads(row["record_json"])
    except (TypeError, json.JSONDecodeError) as exc:
        raise BoundedPlannerConflict(f"{name} record_json is invalid") from exc
    if type(wire) is not dict or canonical_json(wire) != row["record_json"]:
        raise BoundedPlannerConflict(f"{name} record_json is not canonical")
    body = dict(wire)
    asserted = body.pop("content_hash", None)
    if asserted != content_hash(body) or asserted != row["content_hash"]:
        raise BoundedPlannerConflict(f"{name} content hash drifted")
    if wire.get("id") not in {
        row[key] for key in row.keys() if key.endswith("_id") and row[key] is not None
    }:
        raise BoundedPlannerConflict(f"{name} identity column drifted")
    column_bindings = {
        "created_at": "created_at",
        "actor_ref": "actor_ref",
        "template_ref": "template_ref",
        "loop_ref": "loop_ref",
        "question_ref": "question_ref",
        "question_version_ref": "question_version_ref",
        "directive_ref": "directive_ref",
        "loop_version_ref": "loop_version_ref",
        "proposal_ref": "proposal_ref",
        "work_order_ref": "work_order_ref",
        "workflow_version_ref": "workflow_version_ref",
        "terminal_state": "terminal_state",
        "outcome_kind": "outcome_kind",
        "directive_version_ref": "directive_version_ref",
    }
    for column, field in column_bindings.items():
        if column in row.keys() and field in wire and row[column] != wire[field]:
            raise BoundedPlannerConflict(f"{name} SQL column for {field} drifted")
    derived_bindings = {
        "version_number": wire.get("version"),
        "prior_version_id": wire.get("prior_version_ref"),
        "effective_round": wire.get("effective_round"),
        "round_ordinal": wire.get("ordinal", wire.get("round_ordinal")),
        "through_round": wire.get("through_round"),
        "action_kind": wire.get("action", {}).get("kind"),
        "decision": wire.get("decision"),
        "manifest_ref": wire.get("coverage_manifest_ref"),
    }
    for column, expected in derived_bindings.items():
        if column in row.keys() and row[column] != expected:
            raise BoundedPlannerConflict(f"{name} SQL column for {column} drifted")
    return wire


def _validate_budget(value: Any) -> dict[str, int]:
    obj = _closed(value, {"max_rounds", "max_cost_units", "max_seconds"}, "budget")
    return {key: _positive_int(obj[key], f"budget.{key}") for key in obj}


def _validate_parameter_contract(value: Any) -> dict[str, Any]:
    obj = _closed(
        value, {"allowed_fields", "required_fields", "constants"},
        "parameter_contract",
    )
    allowed = _unique_texts(obj["allowed_fields"], "parameter_contract.allowed_fields", nonempty=True)
    required = _unique_texts(obj["required_fields"], "parameter_contract.required_fields")
    if not set(required).issubset(set(allowed)):
        raise BoundedPlannerValidationError("required parameter fields must be allowed")
    if not isinstance(obj["constants"], Mapping):
        raise BoundedPlannerValidationError("parameter_contract.constants must be an object")
    constants = dict(obj["constants"])
    if not set(constants).issubset(set(allowed)):
        raise BoundedPlannerValidationError("constant parameter fields must be allowed")
    return {
        "allowed_fields": allowed,
        "required_fields": required,
        "constants": constants,
    }


def _validate_parameters(contract: Mapping[str, Any], value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise BoundedPlannerValidationError("probe parameters must be an object")
    result = dict(value)
    allowed = set(contract["allowed_fields"])
    required = set(contract["required_fields"])
    if not set(result).issubset(allowed):
        raise BoundedPlannerValidationError(
            f"probe parameters exceed template: {sorted(set(result) - allowed)}"
        )
    if not required.issubset(set(result)):
        raise BoundedPlannerValidationError(
            f"probe parameters omit required fields: {sorted(required - set(result))}"
        )
    for key, expected in contract["constants"].items():
        if result.get(key) != expected:
            raise BoundedPlannerValidationError(
                f"probe parameter {key!r} must equal the template constant"
            )
    for key, item in result.items():
        if isinstance(item, str):
            _text(item, f"parameters.{key}")
        elif isinstance(item, list):
            if not item or not all(isinstance(x, str) and x for x in item):
                raise BoundedPlannerValidationError(
                    f"parameters.{key} list must contain non-empty strings"
                )
        elif item is None or isinstance(item, (bool, int)):
            continue
        else:
            raise BoundedPlannerValidationError(
                f"parameters.{key} has unsupported value type"
            )
    return result


class BoundedPlannerAuthority:
    """Append-only authority for templates, loops, directives and outcomes."""

    def __init__(self, store: Any):
        if not hasattr(store, "connection") or not hasattr(store, "_transaction"):
            raise TypeError("BoundedPlannerAuthority requires a DaltonStore")
        self.store = store
        self.connection: sqlite3.Connection = store.connection
        self.connection.executescript(_SCHEMA_PATH.read_text(encoding="utf-8"))

    def _one(self, table: str, key: str, value: str, name: str) -> dict[str, Any]:
        row = self.connection.execute(
            f"SELECT * FROM {table} WHERE {key}=?", (value,)
        ).fetchone()
        return _decode_record(row, name)

    def probe_template(self, version_ref: str) -> dict[str, Any]:
        return self._one(
            "bounded_probe_template_versions", "version_id", version_ref,
            f"ProbeTemplateVersion {version_ref}",
        )

    def loop(self, version_ref: str) -> dict[str, Any]:
        return self._one(
            "bounded_planner_loop_versions", "version_id", version_ref,
            f"BoundedPlannerLoopVersion {version_ref}",
        )

    def proposal(self, proposal_ref: str) -> dict[str, Any]:
        return self._one(
            "bounded_planner_proposal_versions", "proposal_id", proposal_ref,
            f"PlannerProposalVersion {proposal_ref}",
        )

    def decision_for(self, proposal_ref: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM bounded_planner_proposal_decisions WHERE proposal_ref=?",
            (proposal_ref,),
        ).fetchone()
        return None if row is None else _decode_record(row, "PlannerProposalDecision")

    def round(self, round_ref: str) -> dict[str, Any]:
        return self._one(
            "bounded_research_plan_rounds", "round_id", round_ref,
            f"ResearchPlanRound {round_ref}",
        )

    def outcome_for_round(self, round_ref: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM bounded_research_outcomes WHERE round_ref=?", (round_ref,)
        ).fetchone()
        return None if row is None else _decode_record(row, "ResearchOutcome")

    def terminal(self, loop_version_ref: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM bounded_planner_terminal_events WHERE loop_version_ref=?",
            (loop_version_ref,),
        ).fetchone()
        return None if row is None else _decode_record(row, "BoundedPlannerTerminalEvent")

    def rounds(self, loop_version_ref: str) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT * FROM bounded_research_plan_rounds WHERE loop_version_ref=? "
            "ORDER BY round_ordinal", (loop_version_ref,),
        ).fetchall()
        return [_decode_record(row, "ResearchPlanRound") for row in rows]

    def outcomes(self, loop_version_ref: str) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT * FROM bounded_research_outcomes WHERE loop_version_ref=? "
            "ORDER BY round_ordinal", (loop_version_ref,),
        ).fetchall()
        return [_decode_record(row, "ResearchOutcome") for row in rows]

    def publish_probe_template(
        self,
        template_ref: str,
        *,
        capability_ref: str,
        operation: str,
        runtime_profile_ref: str,
        parameter_contract: Mapping[str, Any],
        output_contract_ref: str,
        verifier_ref: str,
        permission_scope: str,
        declared_side_effects: Sequence[str],
        cost: Mapping[str, Any],
        actor_ref: str,
        prior_version_ref: str | None = None,
    ) -> dict[str, Any]:
        template_ref = _text(template_ref, "template_ref")
        actor_ref = _human(actor_ref)
        capability_ref = _text(capability_ref, "capability_ref")
        operation = _text(operation, "operation")
        runtime_profile_ref = _text(runtime_profile_ref, "runtime_profile_ref")
        output_contract_ref = _text(output_contract_ref, "output_contract_ref")
        verifier_ref = _text(verifier_ref, "verifier_ref")
        permission_scope = _text(permission_scope, "permission_scope")
        side_effects = _unique_texts(declared_side_effects, "declared_side_effects")
        if any(item.startswith("write:") or item.startswith("delete:") for item in side_effects):
            raise BoundedPlannerValidationError("v1 probe templates must be read-only")
        parameter_contract = _validate_parameter_contract(parameter_contract)
        cost_obj = _closed(cost, {"cost_units", "max_attempts", "max_seconds"}, "cost")
        cost_wire = {key: _positive_int(cost_obj[key], f"cost.{key}") for key in cost_obj}
        latest = self.connection.execute(
            "SELECT version_id,version_number FROM bounded_probe_template_versions "
            "WHERE template_ref=? ORDER BY version_number DESC LIMIT 1", (template_ref,),
        ).fetchone()
        if latest is None:
            if prior_version_ref is not None:
                raise BoundedPlannerConflict("first probe template cannot have a prior version")
            version = 1
        else:
            if prior_version_ref != latest["version_id"]:
                raise BoundedPlannerConflict("probe template must continue the latest version")
            version = int(latest["version_number"]) + 1
        identity = {
            "template_ref": template_ref, "version": version,
            "prior_version_ref": prior_version_ref, "capability_ref": capability_ref,
            "operation": operation, "runtime_profile_ref": runtime_profile_ref,
            "parameter_contract": parameter_contract,
            "output_contract_ref": output_contract_ref, "verifier_ref": verifier_ref,
            "permission_scope": permission_scope,
            "declared_side_effects": side_effects, "cost": cost_wire,
        }
        version_id = _deterministic_ref("probe-template-version", identity)
        existing = self.connection.execute(
            "SELECT * FROM bounded_probe_template_versions WHERE version_id=?", (version_id,)
        ).fetchone()
        if existing is not None:
            return {"status": "duplicate", **_decode_record(existing, "ProbeTemplateVersion")}
        wire = _record({
            "schema_version": SCHEMA_VERSION, "id": version_id,
            "created_at": _now(), **identity, "side_effect_class": "read_only",
            "actor_ref": actor_ref,
        })
        with self.store._transaction() as cur:
            cur.execute(
                "INSERT INTO bounded_probe_template_versions "
                "(version_id,template_ref,version_number,prior_version_id,record_json,"
                "content_hash,actor_ref,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (version_id, template_ref, version, prior_version_ref,
                 canonical_json(wire), wire["content_hash"], actor_ref, wire["created_at"]),
            )
        return {"status": "fresh", **wire}

    def create_loop(
        self,
        loop_ref: str,
        *,
        question_version_ref: str,
        template_bindings: Sequence[Mapping[str, Any]],
        required_coverage_items: Sequence[str],
        budget: Mapping[str, Any],
        actor_ref: str,
        doctrine_binding: Mapping[str, Any] | None = None,
        prior_version_ref: str | None = None,
    ) -> dict[str, Any]:
        loop_ref = _text(loop_ref, "loop_ref")
        actor_ref = _human(actor_ref)
        question = read_exact_backlog_question_version(
            self.connection.cursor(), _text(question_version_ref, "question_version_ref")
        )
        required = _unique_texts(required_coverage_items, "required_coverage_items", nonempty=True)
        budget_wire = _validate_budget(budget)
        if doctrine_binding is not None:
            # The slot is intentional.  The Doctrine authority and exact
            # resolver land together in the next slice; accepting an
            # unresolvable ref now would create false governance.
            raise BoundedPlannerValidationError(
                "doctrine_binding is reserved but unsupported until its exact authority resolver exists"
            )
        if not isinstance(template_bindings, (list, tuple)) or not template_bindings:
            raise BoundedPlannerValidationError("template_bindings must not be empty")
        bindings: list[dict[str, Any]] = []
        seen_items: set[str] = set()
        for raw in template_bindings:
            obj = _closed(
                raw, {"coverage_item_ref", "template_version_ref", "parameters"},
                "template_binding",
            )
            coverage_item = _text(obj["coverage_item_ref"], "coverage_item_ref")
            if coverage_item in seen_items:
                raise BoundedPlannerValidationError("coverage items must bind one template each")
            seen_items.add(coverage_item)
            template = self.probe_template(_text(obj["template_version_ref"], "template_version_ref"))
            params = _validate_parameters(template["parameter_contract"], obj["parameters"])
            bindings.append({
                "coverage_item_ref": coverage_item,
                "template_version_ref": template["id"],
                "template_version_hash": template["content_hash"],
                "parameters": params,
            })
        if set(required) != seen_items:
            raise BoundedPlannerValidationError(
                "required_coverage_items must exactly match template bindings"
            )
        latest = self.connection.execute(
            "SELECT version_id,version_number FROM bounded_planner_loop_versions "
            "WHERE loop_ref=? ORDER BY version_number DESC LIMIT 1", (loop_ref,),
        ).fetchone()
        if latest is None:
            if prior_version_ref is not None:
                raise BoundedPlannerConflict("first loop version cannot have a prior version")
            version = 1
        else:
            if prior_version_ref != latest["version_id"]:
                raise BoundedPlannerConflict("loop must continue the latest immutable version")
            version = int(latest["version_number"]) + 1
        identity = {
            "loop_ref": loop_ref, "version": version,
            "prior_version_ref": prior_version_ref,
            "question_ref": question["question_ref"],
            "question_version_ref": question["id"],
            "question_version_hash": question["content_hash"],
            "planner_ref": CAPITAL_LEASE_PLANNER_REF,
            "planner_hash": CAPITAL_LEASE_PLANNER_HASH,
            "template_bindings": bindings,
            "required_coverage_items": required,
            "budget": budget_wire,
            "doctrine_binding": None,
        }
        version_id = _deterministic_ref("bounded-planner-loop-version", identity)
        existing = self.connection.execute(
            "SELECT * FROM bounded_planner_loop_versions WHERE version_id=?", (version_id,)
        ).fetchone()
        if existing is not None:
            return {"status": "duplicate", **_decode_record(existing, "BoundedPlannerLoopVersion")}
        wire = _record({
            "schema_version": SCHEMA_VERSION, "id": version_id,
            "created_at": _now(), **identity, "actor_ref": actor_ref,
        })
        with self.store._transaction() as cur:
            cur.execute(
                "INSERT INTO bounded_planner_loop_versions "
                "(version_id,loop_ref,version_number,prior_version_id,question_ref,"
                "question_version_ref,record_json,content_hash,actor_ref,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (version_id, loop_ref, version, prior_version_ref, question["question_ref"],
                 question["id"], canonical_json(wire), wire["content_hash"], actor_ref,
                 wire["created_at"]),
            )
        return {"status": "fresh", **wire}

    def issue_directive(
        self,
        loop_version_ref: str,
        *,
        verbatim_text: str,
        control_effect: str,
        target_coverage_item_ref: str | None,
        actor_ref: str,
    ) -> dict[str, Any]:
        loop = self.loop(loop_version_ref)
        actor_ref = _human(actor_ref)
        verbatim_text = _text(verbatim_text, "verbatim_text")
        control_effect = _text(control_effect, "control_effect")
        if control_effect not in _CONTROL_EFFECTS:
            raise BoundedPlannerValidationError("control_effect is outside the closed set")
        if control_effect == "focus_coverage_item":
            target = _text(target_coverage_item_ref, "target_coverage_item_ref")
            if target not in loop["required_coverage_items"]:
                raise BoundedPlannerValidationError("directive cannot expand loop coverage")
        elif target_coverage_item_ref is not None:
            raise BoundedPlannerValidationError(
                "target_coverage_item_ref is only valid for focus_coverage_item"
            )
        else:
            target = None
        rounds = self.rounds(loop_version_ref)
        current_round = rounds[-1] if rounds else None
        effective_round = (current_round["ordinal"] + 1) if current_round else 1
        identity = {
            "loop_version_ref": loop["id"], "loop_version_hash": loop["content_hash"],
            "verbatim_text": verbatim_text, "control_effect": control_effect,
            "target_coverage_item_ref": target, "effective_round": effective_round,
        }
        directive_ref = _deterministic_ref("research-directive", identity)
        version_id = _deterministic_ref("research-directive-version", identity)
        existing = self.connection.execute(
            "SELECT * FROM bounded_research_directive_versions WHERE version_id=?",
            (version_id,),
        ).fetchone()
        if existing is not None:
            directive = _decode_record(existing, "ResearchDirectiveVersion")
            receipt = self._one(
                "bounded_research_directive_receipts", "directive_version_ref", version_id,
                "ResearchDirectiveReceipt",
            )
            return {"status": "duplicate", "directive": directive, "receipt": receipt}
        created_at = _now()
        directive = _record({
            "schema_version": SCHEMA_VERSION, "id": version_id,
            "created_at": created_at, "directive_ref": directive_ref,
            "version": 1, "prior_version_ref": None, **identity,
            "actor_ref": actor_ref,
        })
        receipt_id = _deterministic_ref(
            "research-directive-receipt", {"directive_version_ref": version_id}
        )
        receipt = _record({
            "schema_version": SCHEMA_VERSION, "id": receipt_id,
            "created_at": created_at, "directive_version_ref": version_id,
            "directive_version_hash": directive["content_hash"],
            "current_round_ref": current_round["id"] if current_round else None,
            "current_round_unchanged": True, "effective_round": effective_round,
            "actor_ref": "core:bounded-planner",
        })
        with self.store._transaction() as cur:
            cur.execute(
                "INSERT INTO bounded_research_directive_versions "
                "(version_id,directive_ref,loop_version_ref,effective_round,record_json,"
                "content_hash,actor_ref,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (version_id, directive_ref, loop["id"], effective_round,
                 canonical_json(directive), directive["content_hash"], actor_ref, created_at),
            )
            cur.execute(
                "INSERT INTO bounded_research_directive_receipts "
                "(receipt_id,directive_version_ref,record_json,content_hash,created_at) "
                "VALUES(?,?,?,?,?)",
                (receipt_id, version_id, canonical_json(receipt), receipt["content_hash"], created_at),
            )
        return {"status": "fresh", "directive": directive, "receipt": receipt}

    def _directive_bindings(self, loop_version_ref: str, ordinal: int) -> list[dict[str, str]]:
        rows = self.connection.execute(
            "SELECT * FROM bounded_research_directive_versions "
            "WHERE loop_version_ref=? AND effective_round<=? ORDER BY created_at,version_id",
            (loop_version_ref, ordinal),
        ).fetchall()
        return [
            {"directive_version_ref": wire["id"], "directive_version_hash": wire["content_hash"]}
            for wire in (_decode_record(row, "ResearchDirectiveVersion") for row in rows)
        ]

    def _budget_snapshot(self, loop: Mapping[str, Any]) -> dict[str, int]:
        used_rounds = 0
        used_cost = 0
        used_seconds = 0
        for round_wire in self.rounds(loop["id"]):
            proposal = self.proposal(round_wire["proposal_ref"])
            template = self.probe_template(proposal["action"]["template_version_ref"])
            used_rounds += 1
            used_cost += template["cost"]["cost_units"]
            used_seconds += template["cost"]["max_seconds"]
        return {
            "rounds_remaining": max(loop["budget"]["max_rounds"] - used_rounds, 0),
            "cost_units_remaining": max(loop["budget"]["max_cost_units"] - used_cost, 0),
            "seconds_remaining": max(loop["budget"]["max_seconds"] - used_seconds, 0),
        }

    def submit_proposal(
        self,
        loop_version_ref: str,
        *,
        action: Mapping[str, Any],
        rationale: str,
        actor_ref: str,
        planner_context_pack_ref: str | None = None,
        planner_context_pack_hash: str | None = None,
    ) -> dict[str, Any]:
        loop = self.loop(loop_version_ref)
        if self.terminal(loop["id"]) is not None:
            raise BoundedPlannerConflict("terminal loop cannot accept another proposal")
        rounds = self.rounds(loop["id"])
        if rounds and self.outcome_for_round(rounds[-1]["id"]) is None:
            raise BoundedPlannerPending("latest round has no ResearchOutcome")
        ordinal = len(rounds) + 1
        action_wire = self._validate_action(loop, action)
        prior_outcome = self.outcome_for_round(rounds[-1]["id"]) if rounds else None
        directive_bindings = self._directive_bindings(loop["id"], ordinal)
        actor_ref = _text(actor_ref, "actor_ref")
        rationale = _text(rationale, "rationale")
        if (planner_context_pack_ref is None) != (planner_context_pack_hash is None):
            raise BoundedPlannerValidationError(
                "planner context ref and hash must be provided together"
            )
        context = None
        planner_ref = CAPITAL_LEASE_PLANNER_REF
        planner_hash = CAPITAL_LEASE_PLANNER_HASH
        proposal_schema_version = SCHEMA_VERSION
        if planner_context_pack_ref is not None:
            from .research_doctrine import (
                DOCTRINE_AWARE_PLANNER_HASH,
                DOCTRINE_AWARE_PLANNER_REF,
                revalidate_planner_context_pack,
            )

            context = revalidate_planner_context_pack(
                self,
                planner_context_pack_ref,
                expected_hash=planner_context_pack_hash,
            )
            if context["loop_version_ref"] != loop["id"]:
                raise BoundedPlannerValidationError(
                    "planner context belongs to a different loop"
                )
            planner_ref = DOCTRINE_AWARE_PLANNER_REF
            planner_hash = DOCTRINE_AWARE_PLANNER_HASH
            proposal_schema_version = "0.2"
        identity = {
            "loop_version_ref": loop["id"], "loop_version_hash": loop["content_hash"],
            "ordinal": ordinal,
            "prior_outcome_ref": prior_outcome["id"] if prior_outcome else None,
            "prior_outcome_hash": prior_outcome["content_hash"] if prior_outcome else None,
            "action": action_wire,
            "remaining_budget": self._budget_snapshot(loop),
            "directive_bindings": directive_bindings,
            "planner_ref": planner_ref,
            "planner_hash": planner_hash,
        }
        if context is not None:
            identity["planner_context_pack_ref"] = context["id"]
            identity["planner_context_pack_hash"] = context["content_hash"]
        proposal_id = _deterministic_ref("planner-proposal-version", identity)
        existing = self.connection.execute(
            "SELECT * FROM bounded_planner_proposal_versions WHERE proposal_id=?",
            (proposal_id,),
        ).fetchone()
        if existing is not None:
            return {"status": "duplicate", **_decode_record(existing, "PlannerProposalVersion")}
        wire = _record({
            "schema_version": proposal_schema_version, "id": proposal_id,
            "created_at": _now(), **identity, "rationale": rationale,
            "actor_ref": actor_ref,
        })
        with self.store._transaction() as cur:
            cur.execute(
                "INSERT INTO bounded_planner_proposal_versions "
                "(proposal_id,loop_version_ref,round_ordinal,action_kind,record_json,"
                "content_hash,actor_ref,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (proposal_id, loop["id"], ordinal, action_wire["kind"],
                 canonical_json(wire), wire["content_hash"], actor_ref, wire["created_at"]),
            )
        return {"status": "fresh", **wire}

    def _validate_action(
        self, loop: Mapping[str, Any], action: Mapping[str, Any]
    ) -> dict[str, Any]:
        if not isinstance(action, Mapping):
            raise BoundedPlannerValidationError("action must be an object")
        kind = action.get("kind")
        if kind == "probe":
            obj = _closed(
                action,
                {"kind", "coverage_item_ref", "template_version_ref", "template_version_hash", "parameters"},
                "probe action",
            )
            coverage_item = _text(obj["coverage_item_ref"], "coverage_item_ref")
            matches = [
                binding for binding in loop["template_bindings"]
                if binding["coverage_item_ref"] == coverage_item
            ]
            if len(matches) != 1:
                raise BoundedPlannerValidationError("probe coverage item is outside the loop")
            binding = matches[0]
            if (
                obj["template_version_ref"] != binding["template_version_ref"]
                or obj["template_version_hash"] != binding["template_version_hash"]
                or obj["parameters"] != binding["parameters"]
            ):
                raise BoundedPlannerValidationError("probe action drifted from loop authority")
            template = self.probe_template(binding["template_version_ref"])
            if template["content_hash"] != binding["template_version_hash"]:
                raise BoundedPlannerConflict("probe template binding drifted")
            _validate_parameters(template["parameter_contract"], obj["parameters"])
            return dict(obj)
        if kind == "terminate":
            obj = _closed(action, {"kind", "reason"}, "terminate action")
            if obj["reason"] not in _TERMINAL_REASONS:
                raise BoundedPlannerValidationError("terminal reason is outside the closed set")
            return dict(obj)
        raise BoundedPlannerValidationError("action.kind must be probe or terminate")

    def propose_next_capital_lease(self, loop_version_ref: str) -> dict[str, Any]:
        loop = self.loop(loop_version_ref)
        terminal = self.terminal(loop["id"])
        if terminal is not None:
            return {"status": "terminal", "terminal_event": terminal}
        rounds = self.rounds(loop["id"])
        if rounds and self.outcome_for_round(rounds[-1]["id"]) is None:
            return {"status": "pending_round", "round": rounds[-1]}
        ordinal = len(rounds) + 1
        directives = []
        for row in self.connection.execute(
            "SELECT * FROM bounded_research_directive_versions "
            "WHERE loop_version_ref=? AND effective_round<=? ORDER BY created_at,version_id",
            (loop["id"], ordinal),
        ).fetchall():
            directives.append(_decode_record(row, "ResearchDirectiveVersion"))
        if any(item["control_effect"] == "request_replan" for item in directives):
            return self.submit_proposal(
                loop["id"], action={"kind": "terminate", "reason": "human_replan_required"},
                rationale="An admitted human directive requires replanning on this round.",
                actor_ref=CAPITAL_LEASE_PLANNER_REF,
            )
        if any(item["control_effect"] == "deprioritize" for item in directives):
            return self.submit_proposal(
                loop["id"], action={"kind": "terminate", "reason": "human_deprioritized"},
                rationale="An admitted human directive deprioritized this loop.",
                actor_ref=CAPITAL_LEASE_PLANNER_REF,
            )
        outcomes = self.outcomes(loop["id"])
        by_item = {item["coverage_item_ref"]: item for item in outcomes}
        uncovered = [item for item in loop["required_coverage_items"] if item not in by_item]
        if not uncovered:
            reason = (
                "coverage_complete_unobservable_candidate"
                if all(item["outcome_kind"] == "not_found_in_scope" for item in outcomes)
                else "evidence_observed_for_review"
            )
            return self.submit_proposal(
                loop["id"], action={"kind": "terminate", "reason": reason},
                rationale="The governed checklist has reached a closed terminal candidate.",
                actor_ref=CAPITAL_LEASE_PLANNER_REF,
            )
        focused = [
            item["target_coverage_item_ref"] for item in directives
            if item["control_effect"] == "focus_coverage_item"
            and item["target_coverage_item_ref"] in uncovered
        ]
        next_item = focused[-1] if focused else uncovered[0]
        binding = next(
            item for item in loop["template_bindings"]
            if item["coverage_item_ref"] == next_item
        )
        template = self.probe_template(binding["template_version_ref"])
        remaining = self._budget_snapshot(loop)
        if (
            remaining["rounds_remaining"] < 1
            or remaining["cost_units_remaining"] < template["cost"]["cost_units"]
            or remaining["seconds_remaining"] < template["cost"]["max_seconds"]
        ):
            return {
                "status": "terminal",
                "terminal_event": self._append_terminal(
                    loop, "budget_exhausted", proposal=None,
                    actor_ref="core:bounded-planner-budget",
                ),
            }
        return self.submit_proposal(
            loop["id"],
            action={
                "kind": "probe", "coverage_item_ref": next_item,
                "template_version_ref": binding["template_version_ref"],
                "template_version_hash": binding["template_version_hash"],
                "parameters": binding["parameters"],
            },
            rationale="Select the next uncovered item from the admitted capital-lease checklist.",
            actor_ref=CAPITAL_LEASE_PLANNER_REF,
        )

    def propose_next_with_context(self, planner_context_pack_ref: str) -> dict[str, Any]:
        """Use one exact PlannerContextPack without expanding Core authority.

        Human directives retain precedence.  The selected doctrine lens may
        only reorder coverage items that the immutable loop already admitted.
        """

        from .research_doctrine import (
            DOCTRINE_AWARE_PLANNER_REF,
            revalidate_planner_context_pack,
        )

        context = revalidate_planner_context_pack(self, planner_context_pack_ref)
        loop = self.loop(context["loop_version_ref"])
        terminal = self.terminal(loop["id"])
        if terminal is not None:
            return {"status": "terminal", "terminal_event": terminal}
        rounds = self.rounds(loop["id"])
        if rounds and self.outcome_for_round(rounds[-1]["id"]) is None:
            return {"status": "pending_round", "round": rounds[-1]}
        directives = [item["quoted_data"] for item in context["directive_inputs"]]
        proposal_context = {
            "planner_context_pack_ref": context["id"],
            "planner_context_pack_hash": context["content_hash"],
        }
        if any(item["control_effect"] == "request_replan" for item in directives):
            return self.submit_proposal(
                loop["id"], action={"kind": "terminate", "reason": "human_replan_required"},
                rationale="An admitted human directive requires replanning on this round.",
                actor_ref=DOCTRINE_AWARE_PLANNER_REF, **proposal_context,
            )
        if any(item["control_effect"] == "deprioritize" for item in directives):
            return self.submit_proposal(
                loop["id"], action={"kind": "terminate", "reason": "human_deprioritized"},
                rationale="An admitted human directive deprioritized this loop.",
                actor_ref=DOCTRINE_AWARE_PLANNER_REF, **proposal_context,
            )
        outcomes = self.outcomes(loop["id"])
        by_item = {item["coverage_item_ref"]: item for item in outcomes}
        uncovered = [item for item in loop["required_coverage_items"] if item not in by_item]
        if not uncovered:
            reason = (
                "coverage_complete_unobservable_candidate"
                if all(item["outcome_kind"] == "not_found_in_scope" for item in outcomes)
                else "evidence_observed_for_review"
            )
            return self.submit_proposal(
                loop["id"], action={"kind": "terminate", "reason": reason},
                rationale="The governed checklist has reached a closed terminal candidate.",
                actor_ref=DOCTRINE_AWARE_PLANNER_REF, **proposal_context,
            )
        focused = [
            item["target_coverage_item_ref"] for item in directives
            if item["control_effect"] == "focus_coverage_item"
            and item["target_coverage_item_ref"] in uncovered
        ]
        priority = [
            item for item in context["selected_lens"]["priority_topics"]
            if item in uncovered
        ]
        next_item = focused[-1] if focused else (priority[0] if priority else uncovered[0])
        binding = next(
            item for item in loop["template_bindings"]
            if item["coverage_item_ref"] == next_item
        )
        template = self.probe_template(binding["template_version_ref"])
        remaining = self._budget_snapshot(loop)
        if (
            remaining["rounds_remaining"] < 1
            or remaining["cost_units_remaining"] < template["cost"]["cost_units"]
            or remaining["seconds_remaining"] < template["cost"]["max_seconds"]
        ):
            return {
                "status": "terminal",
                "terminal_event": self._append_terminal(
                    loop, "budget_exhausted", proposal=None,
                    actor_ref="core:bounded-planner-budget",
                ),
            }
        return self.submit_proposal(
            loop["id"],
            action={
                "kind": "probe", "coverage_item_ref": next_item,
                "template_version_ref": binding["template_version_ref"],
                "template_version_hash": binding["template_version_hash"],
                "parameters": binding["parameters"],
            },
            rationale=(
                "Select the next uncovered item using the exact quoted doctrine lens; "
                "the lens may reorder but cannot expand the admitted checklist."
            ),
            actor_ref=DOCTRINE_AWARE_PLANNER_REF,
            **proposal_context,
        )

    def _append_terminal(
        self,
        loop: Mapping[str, Any],
        terminal_state: str,
        *,
        proposal: Mapping[str, Any] | None,
        actor_ref: str,
    ) -> dict[str, Any]:
        existing = self.terminal(loop["id"])
        if existing is not None:
            if existing["terminal_state"] != terminal_state:
                raise BoundedPlannerConflict("loop already has another terminal state")
            return existing
        identity = {"loop_version_ref": loop["id"], "terminal_state": terminal_state}
        event_id = _deterministic_ref("bounded-planner-terminal-event", identity)
        wire = _record({
            "schema_version": SCHEMA_VERSION, "id": event_id,
            "created_at": _now(), "loop_version_ref": loop["id"],
            "loop_version_hash": loop["content_hash"],
            "terminal_state": terminal_state,
            "proposal_ref": proposal["id"] if proposal else None,
            "proposal_hash": proposal["content_hash"] if proposal else None,
            "formal_negative_claim_created": False,
            "actor_ref": actor_ref,
        })
        with self.store._transaction() as cur:
            cur.execute(
                "INSERT INTO bounded_planner_terminal_events "
                "(event_id,loop_version_ref,terminal_state,record_json,content_hash,created_at) "
                "VALUES(?,?,?,?,?,?)",
                (event_id, loop["id"], terminal_state, canonical_json(wire),
                 wire["content_hash"], wire["created_at"]),
            )
        return wire


class BoundedPlannerControlPlane:
    """Core admission and execution binding for Bounded Planner proposals."""

    def __init__(
        self,
        authority: BoundedPlannerAuthority,
        observability: Any,
        scheduler: Any,
        *,
        fault_injector: Callable[[str], None] | None = None,
    ) -> None:
        self.authority = authority
        self.observability = observability
        self.scheduler = scheduler
        self.fault_injector = fault_injector
        if not (
            authority.connection is getattr(observability, "connection", None)
            is getattr(scheduler, "connection", None)
        ):
            raise TypeError("bounded authority, observability and scheduler must share one Core connection")

    def _inject(self, seam: str) -> None:
        if self.fault_injector is not None:
            self.fault_injector(seam)

    def _admission_reason(
        self, loop: Mapping[str, Any], proposal: Mapping[str, Any]
    ) -> str | None:
        context_ref = proposal.get("planner_context_pack_ref")
        context_hash = proposal.get("planner_context_pack_hash")
        if (context_ref is None) != (context_hash is None):
            return "planner_context_binding_incomplete"
        if context_ref is not None:
            from .research_doctrine import (
                ResearchDoctrineConflict,
                revalidate_planner_context_pack,
            )

            try:
                context = revalidate_planner_context_pack(
                    self.authority, context_ref, expected_hash=context_hash
                )
            except ResearchDoctrineConflict:
                return "planner_context_binding_stale_or_invalid"
            if context["loop_version_ref"] != loop["id"]:
                return "planner_context_loop_mismatch"
        rounds = self.authority.rounds(loop["id"])
        if proposal["ordinal"] != len(rounds) + 1:
            return "stale_round_ordinal"
        if rounds and self.authority.outcome_for_round(rounds[-1]["id"]) is None:
            return "prior_round_has_no_outcome"
        accepted = self.authority.connection.execute(
            "SELECT p.proposal_id FROM bounded_planner_proposal_versions AS p "
            "JOIN bounded_planner_proposal_decisions AS d ON d.proposal_ref=p.proposal_id "
            "WHERE p.loop_version_ref=? AND p.round_ordinal=? AND d.decision='accepted'",
            (loop["id"], proposal["ordinal"]),
        ).fetchone()
        if accepted is not None and accepted["proposal_id"] != proposal["id"]:
            return "another_proposal_already_accepted"
        action = proposal["action"]
        outcomes = self.authority.outcomes(loop["id"])
        by_item = {item["coverage_item_ref"]: item for item in outcomes}
        if action["kind"] == "probe":
            if action["coverage_item_ref"] in by_item:
                return "coverage_item_already_terminal"
            fingerprint = content_hash({
                "template_version_ref": action["template_version_ref"],
                "parameters": action["parameters"],
            })
            for prior_round in rounds:
                prior = self.authority.proposal(prior_round["proposal_ref"])["action"]
                prior_fingerprint = content_hash({
                    "template_version_ref": prior["template_version_ref"],
                    "parameters": prior["parameters"],
                })
                if fingerprint == prior_fingerprint:
                    return "duplicate_probe"
            template = self.authority.probe_template(action["template_version_ref"])
            remaining = self.authority._budget_snapshot(loop)
            if (
                remaining["rounds_remaining"] < 1
                or remaining["cost_units_remaining"] < template["cost"]["cost_units"]
                or remaining["seconds_remaining"] < template["cost"]["max_seconds"]
            ):
                return "budget_exhausted"
            return None
        reason = action["reason"]
        complete = set(by_item) == set(loop["required_coverage_items"])
        if reason == "coverage_complete_unobservable_candidate":
            if not complete or not outcomes or any(
                item["outcome_kind"] != "not_found_in_scope" for item in outcomes
            ):
                return "negative_terminal_requires_complete_no_match_coverage"
        elif reason == "evidence_observed_for_review":
            if not complete or not any(item["outcome_kind"] == "observed" for item in outcomes):
                return "evidence_terminal_requires_complete_coverage_and_observation"
        elif reason in {"human_replan_required", "human_deprioritized"}:
            required_effect = "request_replan" if reason == "human_replan_required" else "deprioritize"
            directives = self.authority.connection.execute(
                "SELECT * FROM bounded_research_directive_versions "
                "WHERE loop_version_ref=? AND effective_round<=?",
                (loop["id"], proposal["ordinal"]),
            ).fetchall()
            if not any(
                _decode_record(row, "ResearchDirectiveVersion")["control_effect"] == required_effect
                for row in directives
            ):
                return "human_terminal_requires_matching_directive"
        return None

    def admit_proposal(self, proposal_ref: str) -> dict[str, Any]:
        proposal = self.authority.proposal(proposal_ref)
        loop = self.authority.loop(proposal["loop_version_ref"])
        if proposal["loop_version_hash"] != loop["content_hash"]:
            raise BoundedPlannerConflict("proposal loop binding drifted")
        decision = self.authority.decision_for(proposal["id"])
        if decision is None:
            rejection = self._admission_reason(loop, proposal)
            decision_value = "rejected" if rejection else "accepted"
            decision_id = _deterministic_ref(
                "planner-proposal-decision", {"proposal_ref": proposal["id"]}
            )
            decision = _record({
                "schema_version": SCHEMA_VERSION, "id": decision_id,
                # Proposal time makes the accepted WorkOrder stable across a
                # crash between decision and enqueue.
                "created_at": proposal["created_at"],
                "proposal_ref": proposal["id"],
                "proposal_hash": proposal["content_hash"],
                "decision": decision_value,
                "reason": rejection or "closed_contract_and_budget_checks_passed",
                "actor_ref": "core:bounded-planner-admission",
            })
            with self.authority.store._transaction() as cur:
                cur.execute(
                    "INSERT INTO bounded_planner_proposal_decisions "
                    "(decision_id,proposal_ref,decision,record_json,content_hash,created_at) "
                    "VALUES(?,?,?,?,?,?)",
                    (decision_id, proposal["id"], decision_value,
                     canonical_json(decision), decision["content_hash"], decision["created_at"]),
                )
            self._inject("after_decision")
        if decision["proposal_hash"] != proposal["content_hash"]:
            raise BoundedPlannerConflict("proposal decision binding drifted")
        if decision["decision"] == "rejected":
            return {"status": "rejected", "decision": decision}
        if proposal["action"]["kind"] == "terminate":
            terminal = self.authority._append_terminal(
                loop, proposal["action"]["reason"], proposal=proposal,
                actor_ref="core:bounded-planner-admission",
            )
            return {"status": "terminal", "decision": decision, "terminal_event": terminal}
        existing = self.authority.connection.execute(
            "SELECT * FROM bounded_research_plan_rounds WHERE proposal_ref=?",
            (proposal["id"],),
        ).fetchone()
        if existing is not None:
            return {
                "status": "duplicate", "decision": decision,
                "round": _decode_record(existing, "ResearchPlanRound"),
            }
        template = self.authority.probe_template(proposal["action"]["template_version_ref"])
        work_order = self._work_order(loop, proposal, template)
        enqueued = self.scheduler.enqueue(work_order)
        if enqueued["status"] == "conflict":
            raise BoundedPlannerConflict("Scheduler WorkOrder conflicts with proposal authority")
        if enqueued["work_order_hash"] != content_hash(work_order):
            raise BoundedPlannerConflict("Scheduler returned a drifted WorkOrder hash")
        self._inject("after_enqueue")
        rounds = self.authority.rounds(loop["id"])
        ordinal = proposal["ordinal"]
        workflow_ref = _deterministic_ref("workflow:bounded-planner", {"loop": loop["id"]})
        workflow_version_ref = _deterministic_ref(
            "workflow-version:bounded-planner", {"loop": loop["id"], "ordinal": ordinal}
        )
        root_work_ref = rounds[0]["work_order_ref"] if rounds else work_order["id"]
        prior_workflow_ref = rounds[-1]["workflow_version_ref"] if rounds else None
        link_wire = None
        if rounds:
            prior_work_ref = rounds[-1]["work_order_ref"]
            link_id = _deterministic_ref(
                "work-order-link:bounded-planner",
                {"loop": loop["id"], "parent": prior_work_ref, "child": work_order["id"]},
            )
            linked = self.observability.link_work_order(
                workflow_ref, prior_work_ref, work_order["id"], relation="follows_up",
                sequence=ordinal - 1, actor_ref="core:bounded-planner-admission",
                link_id=link_id, idempotency_key=f"bounded-planner-link:{loop['id']}:{ordinal}",
            )
            if linked["status"] == "conflict":
                raise BoundedPlannerConflict("WorkOrderLink conflicts with round authority")
            link_wire = self.observability.work_order_links(workflow_ref)[-1]
            self._inject("after_link")
        workflow = self.observability.create_workflow_version(
            workflow_ref,
            title=f"Bounded research loop {loop['loop_ref']}",
            objective="Execute one admitted probe for the exact research question",
            scope_refs=[loop["question_ref"], loop["question_version_ref"], loop["id"]],
            root_work_order_refs=[root_work_ref], governance_policy_ref=None,
            actor_ref="core:bounded-planner-admission",
            prior_version_ref=prior_workflow_ref,
            version_id=workflow_version_ref,
            idempotency_key=f"bounded-planner-workflow:{loop['id']}:{ordinal}",
        )
        if workflow["status"] == "conflict":
            raise BoundedPlannerConflict("WorkflowRunVersion conflicts with round authority")
        workflow_wire = self.observability.get_workflow_version(workflow_version_ref)
        self._inject("after_workflow")
        round_id = _deterministic_ref(
            "research-plan-round", {"loop": loop["id"], "ordinal": ordinal}
        )
        round_wire = _record({
            "schema_version": SCHEMA_VERSION, "id": round_id,
            "created_at": decision["created_at"], "loop_version_ref": loop["id"],
            "loop_version_hash": loop["content_hash"], "ordinal": ordinal,
            "proposal_ref": proposal["id"], "proposal_hash": proposal["content_hash"],
            "decision_ref": decision["id"], "decision_hash": decision["content_hash"],
            "workflow_ref": workflow_ref, "workflow_version_ref": workflow_version_ref,
            "workflow_version_hash": workflow_wire["content_hash"],
            "work_order_ref": work_order["id"], "work_order_hash": content_hash(work_order),
            "prior_round_ref": rounds[-1]["id"] if rounds else None,
            "work_order_link_ref": link_wire["id"] if link_wire else None,
            "work_order_link_hash": link_wire["content_hash"] if link_wire else None,
            "actor_ref": "core:bounded-planner-admission",
        })
        self._inject("before_round")
        with self.authority.store._transaction() as cur:
            cur.execute(
                "INSERT INTO bounded_research_plan_rounds "
                "(round_id,loop_version_ref,round_ordinal,proposal_ref,work_order_ref,"
                "workflow_version_ref,record_json,content_hash,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (round_id, loop["id"], ordinal, proposal["id"], work_order["id"],
                 workflow_version_ref, canonical_json(round_wire), round_wire["content_hash"],
                 round_wire["created_at"]),
            )
        return {"status": "fresh", "decision": decision, "round": round_wire}

    @staticmethod
    def _work_order(
        loop: Mapping[str, Any], proposal: Mapping[str, Any], template: Mapping[str, Any]
    ) -> dict[str, Any]:
        action = proposal["action"]
        work_id = _deterministic_ref(
            "work:bounded-planner", {"proposal_ref": proposal["id"]}
        )
        wire = {
            "schema_version": SCHEMA_VERSION, "id": work_id,
            "created_at": proposal["created_at"], "updated_at": proposal["created_at"],
            "question": (
                f"Run admitted read-only probe {template['operation']} for "
                f"coverage item {action['coverage_item_ref']}"
            ),
            "requested_capabilities": [template["capability_ref"]],
            "runtime_profile_ref": template["runtime_profile_ref"],
            "budget": dict(template["cost"]),
            "idempotency_key": f"bounded-planner-work:{proposal['id']}",
            "declared_side_effects": list(template["declared_side_effects"]),
            "status": "ready",
            "input_refs": [loop["question_version_ref"], loop["id"], proposal["id"], template["id"]],
            "metadata": {
                "bounded_loop_version_ref": loop["id"],
                "bounded_loop_version_hash": loop["content_hash"],
                "planner_proposal_ref": proposal["id"],
                "planner_proposal_hash": proposal["content_hash"],
                "probe_template_version_ref": template["id"],
                "probe_template_version_hash": template["content_hash"],
                "coverage_item_ref": action["coverage_item_ref"],
                "operation": template["operation"],
                "permission_scope": template["permission_scope"],
                "parameters": action["parameters"],
            },
        }
        return WorkOrder.from_dict(wire).to_dict()

    def record_outcome(self, round_ref: str) -> dict[str, Any]:
        round_wire = self.authority.round(round_ref)
        existing = self.authority.outcome_for_round(round_ref)
        if existing is not None:
            manifest = self.authority._one(
                "bounded_coverage_manifests", "manifest_id", existing["coverage_manifest_ref"],
                "CoverageManifest",
            )
            return {"status": "duplicate", "outcome": existing, "manifest": manifest}
        loop = self.authority.loop(round_wire["loop_version_ref"])
        proposal = self.authority.proposal(round_wire["proposal_ref"])
        template = self.authority.probe_template(proposal["action"]["template_version_ref"])
        work_row = self.authority.connection.execute(
            "SELECT work_order_json,work_order_hash FROM scheduler_work_orders WHERE work_order_id=?",
            (round_wire["work_order_ref"],),
        ).fetchone()
        if work_row is None:
            raise BoundedPlannerNotFound("round WorkOrder is missing from Scheduler")
        expected_work = self._work_order(loop, proposal, template)
        if (
            work_row["work_order_json"] != canonical_json(expected_work)
            or work_row["work_order_hash"] != content_hash(expected_work)
            or round_wire["work_order_hash"] != content_hash(expected_work)
        ):
            raise BoundedPlannerConflict("round WorkOrder drifted from proposal authority")
        formal = self.scheduler.formal_result(round_wire["work_order_ref"])
        if formal is None:
            raise BoundedPlannerPending("round WorkOrder has no formal ResultEnvelope")
        result = formal["result_envelope"]
        if (
            content_hash(result) != formal["result_envelope_hash"]
            or formal["result_envelope_hash"] != round_wire.get("result_envelope_hash", formal["result_envelope_hash"])
        ):
            raise BoundedPlannerConflict("formal ResultEnvelope hash drifted")
        if result["work_order_ref"] != round_wire["work_order_ref"]:
            raise BoundedPlannerConflict("formal ResultEnvelope targets another WorkOrder")
        actual_side_effects = set(result["actual_side_effects"])
        declared_side_effects = set(template["declared_side_effects"])
        if not actual_side_effects.issubset(declared_side_effects):
            raise BoundedPlannerConflict(
                "probe ResultEnvelope reports side effects outside its admitted template"
            )
        if formal["terminal_state"] == "failed":
            outcome_kind = "source_unavailable"
            matches: list[dict[str, Any]] = []
        elif formal["terminal_state"] == "succeeded":
            raw_matches = result["outputs"].get("matches")
            if not isinstance(raw_matches, list) or not all(isinstance(item, Mapping) for item in raw_matches):
                raise BoundedPlannerConflict(
                    "successful probe output must contain a machine-readable matches array"
                )
            matches = [dict(item) for item in raw_matches]
            outcome_kind = "observed" if matches else "not_found_in_scope"
        else:
            raise BoundedPlannerConflict("only terminal Scheduler results can form outcomes")
        locations = []
        for item in matches:
            location = _text(item.get("source_location"), "matches[].source_location")
            if location not in locations:
                locations.append(location)
        action = proposal["action"]
        parameters = action["parameters"]
        source_ref = _text(parameters.get("source_ref"), "parameters.source_ref")
        locator = _text(parameters.get("locator"), "parameters.locator")
        query_terms = _unique_texts(parameters.get("query_terms"), "parameters.query_terms", nonempty=True)
        entry = {
            "coverage_item_ref": action["coverage_item_ref"],
            "round_ref": round_wire["id"],
            "template_version_ref": template["id"],
            "template_version_hash": template["content_hash"],
            "source_ref": source_ref, "locator": locator, "query_terms": query_terms,
            "observation_status": outcome_kind, "match_count": len(matches),
            "matched_source_locations": locations,
            "result_envelope_ref": result["id"],
            "result_envelope_hash": formal["result_envelope_hash"],
        }
        prior_outcomes = self.authority.outcomes(loop["id"])
        if len(prior_outcomes) != round_wire["ordinal"] - 1:
            raise BoundedPlannerConflict("ResearchOutcome history has a gap")
        entries: list[dict[str, Any]] = []
        if prior_outcomes:
            prior_manifest = self.authority._one(
                "bounded_coverage_manifests", "manifest_id",
                prior_outcomes[-1]["coverage_manifest_ref"], "CoverageManifest",
            )
            entries.extend(prior_manifest["entries"])
        if any(item["coverage_item_ref"] == entry["coverage_item_ref"] for item in entries):
            raise BoundedPlannerConflict("coverage item already has a terminal outcome")
        entries.append(entry)
        terminal_items = {
            item["coverage_item_ref"] for item in entries
            if item["observation_status"] in {"observed", "not_found_in_scope"}
        }
        coverage_complete = terminal_items == set(loop["required_coverage_items"])
        negative_candidate_eligible = coverage_complete and all(
            item["observation_status"] == "not_found_in_scope" for item in entries
        )
        created_at = result["created_at"]
        manifest_id = _deterministic_ref(
            "coverage-manifest", {"loop": loop["id"], "through_round": round_wire["ordinal"]}
        )
        manifest = _record({
            "schema_version": SCHEMA_VERSION, "id": manifest_id,
            "created_at": created_at, "loop_version_ref": loop["id"],
            "loop_version_hash": loop["content_hash"],
            "through_round": round_wire["ordinal"], "entries": entries,
            "required_coverage_items": loop["required_coverage_items"],
            "coverage_complete": coverage_complete,
            "negative_candidate_eligible": negative_candidate_eligible,
            "derivation_kind": "core_from_exact_result_envelopes",
            "actor_ref": "core:bounded-planner-coverage",
        })
        outcome_id = _deterministic_ref(
            "research-outcome", {"round_ref": round_wire["id"]}
        )
        outcome = _record({
            "schema_version": SCHEMA_VERSION, "id": outcome_id,
            "created_at": created_at, "loop_version_ref": loop["id"],
            "loop_version_hash": loop["content_hash"], "round_ref": round_wire["id"],
            "round_hash": round_wire["content_hash"], "round_ordinal": round_wire["ordinal"],
            "coverage_item_ref": action["coverage_item_ref"],
            "outcome_kind": outcome_kind,
            "result_envelope_ref": result["id"],
            "result_envelope_hash": formal["result_envelope_hash"],
            "coverage_manifest_ref": manifest_id,
            "coverage_manifest_hash": manifest["content_hash"],
            "formal_claim_refs": [], "formal_negative_claim_created": False,
            "actor_ref": "core:bounded-planner-outcome",
        })
        with self.authority.store._transaction() as cur:
            cur.execute(
                "INSERT INTO bounded_coverage_manifests "
                "(manifest_id,loop_version_ref,through_round,record_json,content_hash,created_at) "
                "VALUES(?,?,?,?,?,?)",
                (manifest_id, loop["id"], round_wire["ordinal"], canonical_json(manifest),
                 manifest["content_hash"], created_at),
            )
            cur.execute(
                "INSERT INTO bounded_research_outcomes "
                "(outcome_id,loop_version_ref,round_ref,round_ordinal,manifest_ref,outcome_kind,"
                "record_json,content_hash,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (outcome_id, loop["id"], round_wire["id"], round_wire["ordinal"],
                 manifest_id, outcome_kind, canonical_json(outcome), outcome["content_hash"],
                 created_at),
            )
        return {"status": "fresh", "outcome": outcome, "manifest": manifest}
