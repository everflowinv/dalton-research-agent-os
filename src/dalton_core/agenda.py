"""Authoritative, replayable Agenda Shadow records.

Agenda decisions are operational records, not research beliefs.  They bypass
the thesis commit gate but remain append-only, human-governed, idempotent, and
fully attributable.  Model-generated feature values are bounded inputs; the
selection function and tie-break are deterministic.
"""

from __future__ import annotations

import json
import math
import sqlite3
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Mapping, Sequence

from .store import canonical_json, content_hash


SCHEMA_VERSION = "0.1"
_SCHEMA_PATH = Path(__file__).with_name("agenda_schema.sql")
FEATURE_NAMES = (
    "mandate_relevance",
    "catalyst_urgency",
    "evidence_staleness",
    "decision_impact",
)
_CYCLE_TRANSITIONS = {
    None: {"collecting"},
    "collecting": {"candidates_ready", "failed"},
    "candidates_ready": {"decided", "failed"},
    "decided": {"delivered", "failed"},
    "delivered": set(),
    "failed": set(),
}


class AgendaError(Exception):
    pass


class AgendaValidationError(AgendaError, ValueError):
    pass


class AgendaConflict(AgendaError):
    pass


class AgendaNotFound(AgendaError):
    pass


class AgendaPaused(AgendaError):
    pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _text(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise AgendaValidationError(f"{name} must be a non-empty string")
    return value.strip()


def _timestamp(value: Any, name: str, *, nullable: bool = False) -> str | None:
    if value is None and nullable:
        return None
    value = _text(value, name)
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise AgendaValidationError(f"{name} must be RFC3339") from exc
    if parsed.tzinfo is None:
        raise AgendaValidationError(f"{name} must include timezone")
    return parsed.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _refs(value: Any, name: str, *, nonempty: bool = False) -> list[str]:
    if not isinstance(value, (list, tuple)):
        raise AgendaValidationError(f"{name} must be an array")
    refs = [_text(item, f"{name}[]") for item in value]
    if nonempty and not refs:
        raise AgendaValidationError(f"{name} must not be empty")
    if len(set(refs)) != len(refs):
        raise AgendaValidationError(f"{name} must contain unique values")
    return refs


def _object(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise AgendaValidationError(f"{name} must be an object")
    return dict(value)


def validate_features(value: Any) -> dict[str, int]:
    features = _object(value, "features")
    if set(features) != set(FEATURE_NAMES):
        raise AgendaValidationError("features must use the frozen Phase 1 schema")
    result: dict[str, int] = {}
    for name in FEATURE_NAMES:
        score = features[name]
        if isinstance(score, bool) or not isinstance(score, int) or not 0 <= score <= 3:
            raise AgendaValidationError(f"feature {name} must be an integer from 0 to 3")
        result[name] = score
    return result


def validate_policy(value: Any) -> dict[str, Any]:
    policy = _object(value, "agenda policy")
    expected = {
        "schema_version", "enabled", "selected_count", "max_model_calls_per_cycle",
        "max_daily_cycles", "max_daily_cost_usd", "max_monthly_cost_usd",
        "max_input_tokens", "max_output_tokens", "feature_weights",
        "trial_company_refs", "cutover_enabled", "cutover_acceptance_threshold",
    }
    if set(policy) != expected or policy.get("schema_version") != SCHEMA_VERSION:
        raise AgendaValidationError("agenda policy has an invalid closed shape")
    for name in (
        "selected_count", "max_model_calls_per_cycle", "max_daily_cycles",
        "max_input_tokens", "max_output_tokens",
    ):
        value = policy[name]
        if isinstance(value, bool) or not isinstance(value, int) or value < 1:
            raise AgendaValidationError(f"{name} must be a positive integer")
    for name in ("max_daily_cost_usd", "max_monthly_cost_usd"):
        amount = policy[name]
        if isinstance(amount, bool) or not isinstance(amount, (int, float)):
            raise AgendaValidationError(f"{name} must be a non-negative finite number")
        if not math.isfinite(float(amount)) or amount < 0:
            raise AgendaValidationError(f"{name} must be a non-negative finite number")
    if not isinstance(policy["enabled"], bool) or not isinstance(policy["cutover_enabled"], bool):
        raise AgendaValidationError("enabled flags must be boolean")
    threshold = policy["cutover_acceptance_threshold"]
    if threshold is not None and (
        isinstance(threshold, bool)
        or not isinstance(threshold, (int, float))
        or not 0 <= float(threshold) <= 1
    ):
        raise AgendaValidationError("cutover_acceptance_threshold must be null or 0..1")
    if policy["cutover_enabled"] and threshold is None:
        raise AgendaValidationError("cutover cannot be enabled without an acceptance threshold")
    policy["trial_company_refs"] = _refs(
        policy["trial_company_refs"], "trial_company_refs", nonempty=True
    )
    weights = _object(policy["feature_weights"], "feature_weights")
    if set(weights) != set(FEATURE_NAMES):
        raise AgendaValidationError("feature_weights must match the frozen feature schema")
    for name, weight in weights.items():
        if isinstance(weight, bool) or not isinstance(weight, int) or not 0 <= weight <= 10:
            raise AgendaValidationError(f"feature weight {name} must be an integer from 0 to 10")
    policy["feature_weights"] = {name: weights[name] for name in FEATURE_NAMES}
    return policy


class AgendaStore:
    """Agenda authority layered on a ``DaltonStore`` transaction boundary."""

    def __init__(self, store: Any):
        if not hasattr(store, "connection") or not hasattr(store, "_transaction"):
            raise TypeError("store must be a DaltonStore-like authority")
        self.store = store
        self.connection: sqlite3.Connection = store.connection
        self.connection.executescript(_SCHEMA_PATH.read_text(encoding="utf-8"))
        self._migrate_phase1_delivery_schema()
        self._ensure_paused_bootstrap()

    def _migrate_phase1_delivery_schema(self) -> None:
        """Add append-only delivery/feedback metadata to pre-bridge databases."""
        extensions = {
            "agenda_outbox_events": {
                "delivery_attempt_id": "TEXT",
                "claim_expires_at": "TEXT",
                "endpoint_ref": "TEXT",
                "retry_after": "TEXT",
            },
            "agenda_feedback": {
                "prior_feedback_id": "TEXT REFERENCES agenda_feedback(feedback_id)",
                "subject_ref": "TEXT",
                "source": "TEXT",
                "source_event_ref": "TEXT",
            },
        }
        for table, columns in extensions.items():
            present = {
                row["name"] for row in self.connection.execute(f"PRAGMA table_info({table})")
            }
            for name, declaration in columns.items():
                if name not in present:
                    self.connection.execute(
                        f"ALTER TABLE {table} ADD COLUMN {name} {declaration}"
                    )
        self.connection.executescript(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_agenda_delivery_receipt_unique "
            "ON agenda_outbox_events(delivery_receipt_id) "
            "WHERE state='delivered' AND delivery_receipt_id IS NOT NULL;"
            "CREATE INDEX IF NOT EXISTS idx_agenda_feedback_subject "
            "ON agenda_feedback(decision_id,subject_ref,created_at);"
        )

    def _ensure_paused_bootstrap(self) -> None:
        if self.connection.execute(
            "SELECT 1 FROM agenda_control_pointer WHERE pointer_id=1"
        ).fetchone():
            return
        self.set_pause(
            True,
            reason="fail-closed bootstrap; human activation required",
            actor_ref="system:bootstrap",
            version_id="agenda-control-version:bootstrap:1",
            idempotency_key="agenda-control:bootstrap",
        )

    @staticmethod
    def _record(base: Mapping[str, Any]) -> dict[str, Any]:
        wire = dict(base)
        wire["content_hash"] = content_hash(wire)
        return wire

    @staticmethod
    def _id(prefix: str, supplied: str | None = None) -> str:
        return _text(supplied, prefix) if supplied is not None else f"{prefix}:{uuid.uuid4().hex}"

    def _idem(self, cur: sqlite3.Cursor, key: str | None, operation: str, request_hash: str) -> dict[str, Any] | None:
        if key is None:
            return None
        key = _text(key, "idempotency_key")
        row = cur.execute(
            "SELECT operation,request_hash,result_json FROM agenda_idempotency WHERE idempotency_key=?",
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
    def _save_idem(cur: sqlite3.Cursor, key: str | None, operation: str, request_hash: str, result: Mapping[str, Any]) -> None:
        if key is None:
            return
        cur.execute(
            "INSERT INTO agenda_idempotency(idempotency_key,operation,request_hash,result_json,created_at) VALUES(?,?,?,?,?)",
            (key, operation, request_hash, canonical_json(result), _now()),
        )

    def _event(self, cur: sqlite3.Cursor, event_type: str, aggregate_ref: str, payload: Mapping[str, Any], actor_ref: str) -> dict[str, Any]:
        created_at = _now()
        wire = self._record({
            "schema_version": SCHEMA_VERSION,
            "id": f"agenda-event:{uuid.uuid4().hex}",
            "event_type": _text(event_type, "event_type"),
            "aggregate_ref": _text(aggregate_ref, "aggregate_ref"),
            "payload": dict(payload),
            "actor_ref": _text(actor_ref, "actor_ref"),
            "created_at": created_at,
        })
        cur.execute(
            "INSERT INTO agenda_domain_events(event_id,event_type,aggregate_ref,payload_json,actor_ref,created_at,content_hash) VALUES(?,?,?,?,?,?,?)",
            (wire["id"], wire["event_type"], wire["aggregate_ref"], canonical_json(wire["payload"]), wire["actor_ref"], created_at, wire["content_hash"]),
        )
        return wire

    def control_state(self) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT v.record_json FROM agenda_control_pointer p JOIN agenda_control_versions v ON v.version_id=p.version_id WHERE p.pointer_id=1"
        ).fetchone()
        if row is None:
            raise AgendaNotFound("agenda control state")
        return json.loads(row["record_json"])

    def set_pause(self, paused: bool, *, reason: str, actor_ref: str, version_id: str | None = None, idempotency_key: str | None = None) -> dict[str, Any]:
        if not isinstance(paused, bool):
            raise AgendaValidationError("paused must be boolean")
        reason = _text(reason, "reason")
        actor_ref = _text(actor_ref, "actor_ref")
        version_id = self._id("agenda-control-version", version_id)
        request = {"paused": paused, "reason": reason, "actor_ref": actor_ref, "version_id": version_id}
        request_hash = content_hash(request)
        with self.store._transaction() as cur:
            duplicate = self._idem(cur, idempotency_key, "set_pause", request_hash)
            if duplicate is not None:
                return duplicate
            latest = cur.execute(
                "SELECT v.* FROM agenda_control_pointer p JOIN agenda_control_versions v ON v.version_id=p.version_id WHERE p.pointer_id=1"
            ).fetchone()
            version_number = 1 if latest is None else int(latest["version_number"]) + 1
            prior = None if latest is None else latest["version_id"]
            wire = self._record({
                "schema_version": SCHEMA_VERSION, "id": version_id,
                "version": version_number, "prior_version_ref": prior,
                "paused": paused, "reason": reason, "actor_ref": actor_ref,
                "created_at": _now(),
            })
            cur.execute(
                "INSERT INTO agenda_control_versions(version_id,version_number,prior_version_id,paused,reason,actor_ref,record_json,content_hash,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (version_id, version_number, prior, int(paused), reason, actor_ref, canonical_json(wire), wire["content_hash"], wire["created_at"]),
            )
            cur.execute(
                "INSERT INTO agenda_control_pointer(pointer_id,version_id) VALUES(1,?) ON CONFLICT(pointer_id) DO UPDATE SET version_id=excluded.version_id",
                (version_id,),
            )
            self._event(cur, "agenda_paused" if paused else "agenda_resumed", "agenda-control", {"version_ref": version_id, "reason": reason}, actor_ref)
            result = {"status": "fresh", **wire}
            self._save_idem(cur, idempotency_key, "set_pause", request_hash, result)
            return result

    def create_policy(self, policy: Mapping[str, Any], *, effective_from: str, effective_until: str | None, actor_ref: str, activate: bool = True, version_id: str | None = None, idempotency_key: str | None = None) -> dict[str, Any]:
        policy = validate_policy(policy)
        effective_from = _timestamp(effective_from, "effective_from")  # type: ignore[assignment]
        effective_until = _timestamp(effective_until, "effective_until", nullable=True)
        if effective_until is not None and effective_until <= effective_from:
            raise AgendaValidationError("effective_until must be after effective_from")
        actor_ref = _text(actor_ref, "actor_ref")
        version_id = self._id("agenda-policy-version", version_id)
        request = {"policy": policy, "effective_from": effective_from, "effective_until": effective_until, "actor_ref": actor_ref, "activate": activate, "version_id": version_id}
        request_hash = content_hash(request)
        with self.store._transaction() as cur:
            duplicate = self._idem(cur, idempotency_key, "create_agenda_policy", request_hash)
            if duplicate is not None:
                return duplicate
            latest = cur.execute("SELECT * FROM agenda_policy_versions ORDER BY version_number DESC LIMIT 1").fetchone()
            version_number = 1 if latest is None else int(latest["version_number"]) + 1
            prior = None if latest is None else latest["version_id"]
            created_at = _now()
            wire = self._record({
                "schema_version": SCHEMA_VERSION, "id": version_id, "version": version_number,
                "prior_version_ref": prior, "enabled": policy["enabled"],
                "effective_from": effective_from, "effective_until": effective_until,
                "policy": policy, "actor_ref": actor_ref, "created_at": created_at,
            })
            cur.execute(
                "INSERT INTO agenda_policy_versions(version_id,version_number,prior_version_id,enabled,effective_from,effective_until,policy_json,content_hash,actor_ref,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (version_id, version_number, prior, int(policy["enabled"]), effective_from, effective_until, canonical_json(wire), wire["content_hash"], actor_ref, created_at),
            )
            if activate:
                cur.execute(
                    "INSERT INTO agenda_policy_pointer(pointer_id,version_id) VALUES(1,?) ON CONFLICT(pointer_id) DO UPDATE SET version_id=excluded.version_id",
                    (version_id,),
                )
            self._event(cur, "agenda_policy_created", version_id, {"activated": activate}, actor_ref)
            result = {"status": "fresh", **wire}
            self._save_idem(cur, idempotency_key, "create_agenda_policy", request_hash, result)
            return result

    def active_policy(self, *, at: str | None = None) -> dict[str, Any]:
        at = _timestamp(at or _now(), "at")  # type: ignore[assignment]
        row = self.connection.execute(
            "SELECT v.policy_json FROM agenda_policy_pointer p JOIN agenda_policy_versions v ON v.version_id=p.version_id WHERE p.pointer_id=1 AND v.effective_from<=? AND (v.effective_until IS NULL OR v.effective_until>?)",
            (at, at),
        ).fetchone()
        if row is None:
            raise AgendaNotFound("active agenda policy")
        return json.loads(row["policy_json"])

    def _policy_version(self, version_id: str) -> dict[str, Any]:
        version_id = _text(version_id, "policy_version_ref")
        row = self.connection.execute(
            "SELECT policy_json FROM agenda_policy_versions WHERE version_id=?",
            (version_id,),
        ).fetchone()
        if row is None:
            raise AgendaNotFound(version_id)
        policy = json.loads(row["policy_json"])
        if policy.get("id") != version_id:
            raise AgendaConflict("agenda policy row does not match its frozen version ref")
        return policy

    def budget_status(self, *, daily_since: str, monthly_since: str) -> dict[str, Any]:
        daily_since = _timestamp(daily_since, "daily_since")  # type: ignore[assignment]
        monthly_since = _timestamp(monthly_since, "monthly_since")  # type: ignore[assignment]
        cycle_count = self.connection.execute(
            "SELECT COUNT(*) FROM agenda_cycles c WHERE c.created_at>=? AND "
            "(SELECT e.state FROM agenda_cycle_events e WHERE e.cycle_id=c.cycle_id ORDER BY e.event_seq DESC LIMIT 1)!='failed'",
            (daily_since,),
        ).fetchone()[0]
        # Only the latest immutable cost correction for each usage entry counts.
        rows = self.connection.execute(
            "SELECT c.amount_micros,c.currency,c.created_at FROM observability_cost_entries c "
            "WHERE c.revision_number=(SELECT MAX(x.revision_number) FROM observability_cost_entries x WHERE x.usage_entry_ref=c.usage_entry_ref) "
            "AND c.created_at>=?",
            (monthly_since,),
        ).fetchall()
        daily_micros = 0
        monthly_micros = 0
        unpriced = 0
        for row in rows:
            if row["currency"] != "USD" or row["amount_micros"] is None:
                unpriced += 1
                continue
            amount = int(row["amount_micros"])
            monthly_micros += amount
            if row["created_at"] >= daily_since:
                daily_micros += amount
        return {
            "daily_cycle_count": int(cycle_count),
            "daily_cost_micros": daily_micros,
            "monthly_cost_micros": monthly_micros,
            "unpriced_cost_entries": unpriced,
        }

    def create_mandate(self, mandate_ref: str, *, objective: str, scope_refs: Sequence[str], constraints: Mapping[str, Any], success_criteria: Mapping[str, Any], effective_from: str, effective_until: str | None, actor_ref: str, activate: bool = True, version_id: str | None = None, idempotency_key: str | None = None) -> dict[str, Any]:
        mandate_ref = _text(mandate_ref, "mandate_ref")
        objective = _text(objective, "objective")
        scopes = _refs(scope_refs, "scope_refs", nonempty=True)
        constraints = _object(constraints, "constraints")
        success_criteria = _object(success_criteria, "success_criteria")
        effective_from = _timestamp(effective_from, "effective_from")  # type: ignore[assignment]
        effective_until = _timestamp(effective_until, "effective_until", nullable=True)
        actor_ref = _text(actor_ref, "actor_ref")
        version_id = self._id("mandate-version", version_id)
        request = {
            "mandate_ref": mandate_ref, "objective": objective, "scope_refs": scopes,
            "constraints": constraints, "success_criteria": success_criteria,
            "effective_from": effective_from, "effective_until": effective_until,
            "actor_ref": actor_ref, "activate": activate, "version_id": version_id,
        }
        request_hash = content_hash(request)
        with self.store._transaction() as cur:
            duplicate = self._idem(cur, idempotency_key, "create_mandate", request_hash)
            if duplicate is not None:
                return duplicate
            latest = cur.execute("SELECT * FROM mandate_versions WHERE mandate_ref=? ORDER BY version_number DESC LIMIT 1", (mandate_ref,)).fetchone()
            version_number = 1 if latest is None else int(latest["version_number"]) + 1
            prior = None if latest is None else latest["version_id"]
            created_at = _now()
            wire = self._record({
                "schema_version": SCHEMA_VERSION, "id": version_id, "mandate_ref": mandate_ref,
                "version": version_number, "prior_version_ref": prior, "objective": objective,
                "scope_refs": scopes, "constraints": constraints, "success_criteria": success_criteria,
                "effective_from": effective_from, "effective_until": effective_until,
                "actor_ref": actor_ref, "created_at": created_at,
            })
            cur.execute(
                "INSERT INTO mandate_versions(version_id,mandate_ref,version_number,prior_version_id,objective,scope_refs_json,constraints_json,success_criteria_json,effective_from,effective_until,actor_ref,record_json,content_hash,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (version_id, mandate_ref, version_number, prior, objective, canonical_json(scopes), canonical_json(constraints), canonical_json(success_criteria), effective_from, effective_until, actor_ref, canonical_json(wire), wire["content_hash"], created_at),
            )
            if activate:
                cur.execute(
                    "INSERT INTO mandate_pointer(mandate_ref,version_id,active) VALUES(?,?,1) ON CONFLICT(mandate_ref) DO UPDATE SET version_id=excluded.version_id,active=1",
                    (mandate_ref, version_id),
                )
            self._event(cur, "mandate_created", mandate_ref, {"version_ref": version_id, "activated": activate}, actor_ref)
            result = {"status": "fresh", **wire}
            self._save_idem(cur, idempotency_key, "create_mandate", request_hash, result)
            return result

    def active_mandates(self, *, at: str | None = None) -> list[dict[str, Any]]:
        at = _timestamp(at or _now(), "at")  # type: ignore[assignment]
        rows = self.connection.execute(
            "SELECT v.record_json FROM mandate_pointer p JOIN mandate_versions v ON v.version_id=p.version_id WHERE p.active=1 AND v.effective_from<=? AND (v.effective_until IS NULL OR v.effective_until>?) ORDER BY v.mandate_ref",
            (at, at),
        ).fetchall()
        return [json.loads(row["record_json"]) for row in rows]

    def create_priority_override(self, override_ref: str, *, scope_refs: Sequence[str], weight_deltas: Mapping[str, Any], rationale: str, effective_from: str, effective_until: str, revoked: bool, actor_ref: str, version_id: str | None = None, idempotency_key: str | None = None) -> dict[str, Any]:
        override_ref = _text(override_ref, "override_ref")
        scopes = _refs(scope_refs, "scope_refs", nonempty=True)
        deltas = _object(weight_deltas, "weight_deltas")
        if set(deltas) - set(FEATURE_NAMES):
            raise AgendaValidationError("weight_deltas contains an unknown feature")
        for name, delta in deltas.items():
            if isinstance(delta, bool) or not isinstance(delta, int) or not -10 <= delta <= 10:
                raise AgendaValidationError(f"weight delta {name} must be -10..10")
        rationale = _text(rationale, "rationale")
        effective_from = _timestamp(effective_from, "effective_from")  # type: ignore[assignment]
        effective_until = _timestamp(effective_until, "effective_until")  # type: ignore[assignment]
        if effective_until <= effective_from:
            raise AgendaValidationError("override effective_until must be after effective_from")
        if not isinstance(revoked, bool):
            raise AgendaValidationError("revoked must be boolean")
        actor_ref = _text(actor_ref, "actor_ref")
        version_id = self._id("priority-override-version", version_id)
        request = {"override_ref": override_ref, "scope_refs": scopes, "weight_deltas": deltas, "rationale": rationale, "effective_from": effective_from, "effective_until": effective_until, "revoked": revoked, "actor_ref": actor_ref, "version_id": version_id}
        request_hash = content_hash(request)
        with self.store._transaction() as cur:
            duplicate = self._idem(cur, idempotency_key, "create_priority_override", request_hash)
            if duplicate is not None:
                return duplicate
            latest = cur.execute("SELECT * FROM priority_override_versions WHERE override_ref=? ORDER BY version_number DESC LIMIT 1", (override_ref,)).fetchone()
            version_number = 1 if latest is None else int(latest["version_number"]) + 1
            prior = None if latest is None else latest["version_id"]
            created_at = _now()
            wire = self._record({"schema_version": SCHEMA_VERSION, "id": version_id, "override_ref": override_ref, "version": version_number, "prior_version_ref": prior, "scope_refs": scopes, "weight_deltas": deltas, "rationale": rationale, "effective_from": effective_from, "effective_until": effective_until, "revoked": revoked, "actor_ref": actor_ref, "created_at": created_at})
            cur.execute(
                "INSERT INTO priority_override_versions(version_id,override_ref,version_number,prior_version_id,scope_refs_json,weight_deltas_json,rationale,effective_from,effective_until,revoked,actor_ref,record_json,content_hash,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (version_id, override_ref, version_number, prior, canonical_json(scopes), canonical_json(deltas), rationale, effective_from, effective_until, int(revoked), actor_ref, canonical_json(wire), wire["content_hash"], created_at),
            )
            cur.execute("INSERT INTO priority_override_pointer(override_ref,version_id) VALUES(?,?) ON CONFLICT(override_ref) DO UPDATE SET version_id=excluded.version_id", (override_ref, version_id))
            self._event(cur, "priority_override_changed", override_ref, {"version_ref": version_id, "revoked": revoked}, actor_ref)
            result = {"status": "fresh", **wire}
            self._save_idem(cur, idempotency_key, "create_priority_override", request_hash, result)
            return result

    def active_priority_overrides(self, *, scope_ref: str, at: str | None = None) -> list[dict[str, Any]]:
        scope_ref = _text(scope_ref, "scope_ref")
        at = _timestamp(at or _now(), "at")  # type: ignore[assignment]
        rows = self.connection.execute(
            "SELECT v.record_json FROM priority_override_pointer p JOIN priority_override_versions v ON v.version_id=p.version_id WHERE v.revoked=0 AND v.effective_from<=? AND v.effective_until>? ORDER BY v.override_ref",
            (at, at),
        ).fetchall()
        values = [json.loads(row["record_json"]) for row in rows]
        return [value for value in values if scope_ref in value["scope_refs"]]

    def start_cycle(self, cycle_key: str, *, perception_snapshot_ref: str, perception_snapshot_hash: str, mandate_version_ref: str, policy_version_ref: str, company_ref: str, actor_ref: str, cycle_id: str | None = None, idempotency_key: str | None = None) -> dict[str, Any]:
        cycle_key = _text(cycle_key, "cycle_key")
        perception_snapshot_ref = _text(perception_snapshot_ref, "perception_snapshot_ref")
        perception_snapshot_hash = _text(perception_snapshot_hash, "perception_snapshot_hash")
        if len(perception_snapshot_hash) != 64:
            raise AgendaValidationError("perception_snapshot_hash must be SHA-256")
        mandate_version_ref = _text(mandate_version_ref, "mandate_version_ref")
        policy_version_ref = _text(policy_version_ref, "policy_version_ref")
        company_ref = _text(company_ref, "company_ref")
        actor_ref = _text(actor_ref, "actor_ref")
        cycle_id = self._id("agenda-cycle", cycle_id)
        request = {"cycle_key": cycle_key, "perception_snapshot_ref": perception_snapshot_ref, "perception_snapshot_hash": perception_snapshot_hash, "mandate_version_ref": mandate_version_ref, "policy_version_ref": policy_version_ref, "company_ref": company_ref, "actor_ref": actor_ref, "cycle_id": cycle_id}
        request_hash = content_hash(request)
        with self.store._transaction() as cur:
            duplicate = self._idem(cur, idempotency_key, "start_agenda_cycle", request_hash)
            if duplicate is not None:
                return duplicate
            existing = cur.execute("SELECT cycle_id,content_hash FROM agenda_cycles WHERE cycle_key=?", (cycle_key,)).fetchone()
            if existing is not None:
                return {"status": "duplicate" if existing["content_hash"] == request_hash else "conflict", "cycle_id": existing["cycle_id"]}
            created_at = _now()
            cur.execute(
                "INSERT INTO agenda_cycles(cycle_id,cycle_key,perception_snapshot_ref,perception_snapshot_hash,mandate_version_ref,policy_version_ref,company_ref,created_at,content_hash) VALUES(?,?,?,?,?,?,?,?,?)",
                (cycle_id, cycle_key, perception_snapshot_ref, perception_snapshot_hash, mandate_version_ref, policy_version_ref, company_ref, created_at, request_hash),
            )
            event = self._cycle_event(cur, cycle_id, "collecting", "cycle_started", {"snapshot_ref": perception_snapshot_ref}, actor_ref)
            self._event(cur, "agenda_cycle_started", cycle_id, {"cycle_key": cycle_key}, actor_ref)
            result = {"status": "fresh", "cycle_id": cycle_id, "cycle_key": cycle_key, "event": event, "content_hash": request_hash}
            self._save_idem(cur, idempotency_key, "start_agenda_cycle", request_hash, result)
            return result

    def _latest_cycle_state(self, cur: sqlite3.Cursor, cycle_id: str) -> str | None:
        row = cur.execute("SELECT state FROM agenda_cycle_events WHERE cycle_id=? ORDER BY event_seq DESC LIMIT 1", (cycle_id,)).fetchone()
        return None if row is None else row["state"]

    def _cycle_event(self, cur: sqlite3.Cursor, cycle_id: str, state: str, reason: str, metadata: Mapping[str, Any], actor_ref: str) -> dict[str, Any]:
        prior = self._latest_cycle_state(cur, cycle_id)
        if state not in _CYCLE_TRANSITIONS.get(prior, set()):
            raise AgendaConflict(f"agenda cycle transition {prior!r} -> {state!r} is invalid")
        created_at = _now()
        wire = self._record({"schema_version": SCHEMA_VERSION, "id": f"agenda-cycle-event:{uuid.uuid4().hex}", "cycle_ref": cycle_id, "state": state, "reason": _text(reason, "reason"), "metadata": dict(metadata), "actor_ref": _text(actor_ref, "actor_ref"), "created_at": created_at})
        cur.execute("INSERT INTO agenda_cycle_events(event_id,cycle_id,state,reason,metadata_json,actor_ref,created_at,content_hash) VALUES(?,?,?,?,?,?,?,?)", (wire["id"], cycle_id, state, wire["reason"], canonical_json(wire["metadata"]), wire["actor_ref"], created_at, wire["content_hash"]))
        return wire

    def fail_cycle(self, cycle_id: str, *, reason: str, metadata: Mapping[str, Any], actor_ref: str) -> dict[str, Any]:
        with self.store._transaction() as cur:
            return self._cycle_event(cur, _text(cycle_id, "cycle_id"), "failed", reason, metadata, actor_ref)

    def add_candidates(self, cycle_id: str, *, candidates: Sequence[Mapping[str, Any]], actor_ref: str, idempotency_key: str) -> dict[str, Any]:
        cycle_id = _text(cycle_id, "cycle_id")
        actor_ref = _text(actor_ref, "actor_ref")
        normalized: list[dict[str, Any]] = []
        for index, raw in enumerate(candidates):
            item = _object(raw, f"candidates[{index}]")
            expected = {"candidate_id", "company_ref", "question", "answer_criteria", "features", "rationale", "source_refs"}
            if set(item) != expected:
                raise AgendaValidationError("candidate has an invalid closed shape")
            candidate_id = _text(item["candidate_id"], "candidate_id")
            normalized.append({
                "candidate_id": candidate_id,
                "company_ref": _text(item["company_ref"], "company_ref"),
                "question": _text(item["question"], "question"),
                "answer_criteria": _text(item["answer_criteria"], "answer_criteria"),
                "features": validate_features(item["features"]),
                "rationale": _text(item["rationale"], "rationale"),
                "source_refs": _refs(item["source_refs"], "source_refs", nonempty=True),
            })
        if not normalized:
            raise AgendaValidationError("at least one candidate is required")
        if len({item["candidate_id"] for item in normalized}) != len(normalized):
            raise AgendaValidationError("candidate ids must be unique")
        request_hash = content_hash({"cycle_id": cycle_id, "candidates": normalized, "actor_ref": actor_ref})
        with self.store._transaction() as cur:
            duplicate = self._idem(cur, idempotency_key, "add_agenda_candidates", request_hash)
            if duplicate is not None:
                return duplicate
            if self._latest_cycle_state(cur, cycle_id) != "collecting":
                raise AgendaConflict("cycle is not collecting candidates")
            refs: list[str] = []
            created_at = _now()
            for item in normalized:
                question_ref = "research-question:" + content_hash({"company_ref": item["company_ref"], "question": item["question"]})[:32]
                question_version_ref = f"research-question-version:{content_hash({**item, 'cycle_id': cycle_id})[:32]}"
                prior_row = cur.execute("SELECT v.* FROM research_question_pointer p JOIN research_question_versions v ON v.version_id=p.version_id WHERE p.question_ref=?", (question_ref,)).fetchone()
                if prior_row is None:
                    version_number, prior = 1, None
                    question_wire = self._record({"schema_version": SCHEMA_VERSION, "id": question_version_ref, "question_ref": question_ref, "version": 1, "prior_version_ref": None, "company_ref": item["company_ref"], "question": item["question"], "answer_criteria": item["answer_criteria"], "state": "open", "defer_until": None, "wake_condition": None, "source_refs": item["source_refs"], "actor_ref": actor_ref, "created_at": created_at})
                    cur.execute("INSERT INTO research_question_versions(version_id,question_ref,version_number,prior_version_id,company_ref,question,answer_criteria,state,defer_until,wake_condition,source_refs_json,actor_ref,record_json,content_hash,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (question_version_ref, question_ref, version_number, prior, item["company_ref"], item["question"], item["answer_criteria"], "open", None, None, canonical_json(item["source_refs"]), actor_ref, canonical_json(question_wire), question_wire["content_hash"], created_at))
                    cur.execute("INSERT INTO research_question_pointer(question_ref,version_id) VALUES(?,?)", (question_ref, question_version_ref))
                else:
                    question_version_ref = prior_row["version_id"]
                candidate_wire = self._record({"schema_version": SCHEMA_VERSION, "id": item["candidate_id"], "cycle_ref": cycle_id, "question_version_ref": question_version_ref, "proposed_question": item["question"], "answer_criteria": item["answer_criteria"], "features": item["features"], "rationale": item["rationale"], "source_refs": item["source_refs"], "valid": True, "rejection_reason": None, "actor_ref": actor_ref, "created_at": created_at})
                cur.execute("INSERT INTO agenda_candidates(candidate_id,cycle_id,question_version_ref,proposed_question,answer_criteria,features_json,rationale,source_refs_json,valid,rejection_reason,actor_ref,created_at,content_hash) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)", (item["candidate_id"], cycle_id, question_version_ref, item["question"], item["answer_criteria"], canonical_json(item["features"]), item["rationale"], canonical_json(item["source_refs"]), 1, None, actor_ref, created_at, candidate_wire["content_hash"]))
                refs.append(item["candidate_id"])
            event = self._cycle_event(cur, cycle_id, "candidates_ready", "candidate_generation_completed", {"candidate_refs": refs}, actor_ref)
            result = {"status": "fresh", "cycle_id": cycle_id, "candidate_refs": refs, "event": event}
            self._save_idem(cur, idempotency_key, "add_agenda_candidates", request_hash, result)
            return result

    def decide_cycle(self, cycle_id: str, *, actor_ref: str, idempotency_key: str, decision_id: str | None = None) -> dict[str, Any]:
        cycle_id = _text(cycle_id, "cycle_id")
        actor_ref = _text(actor_ref, "actor_ref")
        decision_id = self._id("agenda-decision", decision_id)
        cycle = self.connection.execute("SELECT * FROM agenda_cycles WHERE cycle_id=?", (cycle_id,)).fetchone()
        if cycle is None:
            raise AgendaNotFound(cycle_id)
        policy_wire = self._policy_version(cycle["policy_version_ref"])
        policy = policy_wire["policy"]
        overrides = self.active_priority_overrides(scope_ref=cycle["company_ref"])
        weights = dict(policy["feature_weights"])
        for override in overrides:
            for name, delta in override["weight_deltas"].items():
                weights[name] = max(0, min(20, weights[name] + delta))
        request = {"cycle_id": cycle_id, "policy_version_ref": policy_wire["id"], "weights": weights, "actor_ref": actor_ref, "decision_id": decision_id}
        request_hash = content_hash(request)
        with self.store._transaction() as cur:
            duplicate = self._idem(cur, idempotency_key, "decide_agenda_cycle", request_hash)
            if duplicate is not None:
                return duplicate
            if self._latest_cycle_state(cur, cycle_id) != "candidates_ready":
                raise AgendaConflict("cycle is not ready for a decision")
            rows = cur.execute("SELECT * FROM agenda_candidates WHERE cycle_id=? ORDER BY candidate_id", (cycle_id,)).fetchall()
            valid = []
            rejected = []
            breakdown: dict[str, Any] = {}
            for row in rows:
                if not row["valid"]:
                    rejected.append(row["candidate_id"])
                    continue
                features = json.loads(row["features_json"])
                contributions = {name: features[name] * weights[name] for name in FEATURE_NAMES}
                score = sum(contributions.values())
                breakdown[row["candidate_id"]] = {"features": features, "weights": weights, "contributions": contributions, "total": score}
                valid.append((score, row["candidate_id"], row))
            valid.sort(key=lambda item: (-item[0], item[1]))
            selected_rows = [row for _, _, row in valid[: policy["selected_count"]]]
            deferred_rows = [row for _, _, row in valid[policy["selected_count"] :]]
            selected = [row["candidate_id"] for row in selected_rows]
            deferred = [row["candidate_id"] for row in deferred_rows]
            created_at = _now()
            wire = self._record({"schema_version": SCHEMA_VERSION, "id": decision_id, "cycle_ref": cycle_id, "selected_candidate_refs": selected, "deferred_candidate_refs": deferred, "rejected_candidate_refs": rejected, "score_breakdown": breakdown, "policy_version_ref": policy_wire["id"], "actor_ref": actor_ref, "created_at": created_at})
            cur.execute("INSERT INTO agenda_decisions(decision_id,cycle_id,selected_candidate_refs_json,deferred_candidate_refs_json,rejected_candidate_refs_json,score_breakdown_json,policy_version_ref,actor_ref,created_at,content_hash) VALUES(?,?,?,?,?,?,?,?,?,?)", (decision_id, cycle_id, canonical_json(selected), canonical_json(deferred), canonical_json(rejected), canonical_json(breakdown), policy_wire["id"], actor_ref, created_at, wire["content_hash"]))
            event = self._cycle_event(cur, cycle_id, "decided", "deterministic_selection_completed", {"decision_ref": decision_id}, actor_ref)
            selected_payload = [{"candidate_ref": row["candidate_id"], "question": row["proposed_question"], "answer_criteria": row["answer_criteria"], "rationale": row["rationale"], "score": breakdown[row["candidate_id"]]["total"]} for row in selected_rows]
            message_id = f"agenda-message:{content_hash({'decision_id': decision_id})[:32]}"
            payload = {"schema_version": SCHEMA_VERSION, "kind": "agenda_shadow_card", "cycle_ref": cycle_id, "decision_ref": decision_id, "company_ref": cycle["company_ref"], "selected": selected_payload, "deferred_count": len(deferred), "rejected_count": len(rejected), "created_at": created_at}
            payload_hash = content_hash(payload)
            cur.execute("INSERT INTO agenda_outbox_messages(message_id,idempotency_key,topic,payload_json,payload_hash,created_at) VALUES(?,?,?,?,?,?)", (message_id, f"agenda-card:{decision_id}", "agenda.shadow.decision", canonical_json(payload), payload_hash, created_at))
            outbox_event = self._outbox_event(cur, message_id, "pending", actor_ref=actor_ref)
            self._event(cur, "agenda_cycle_decided", cycle_id, {"decision_ref": decision_id, "outbox_message_ref": message_id}, actor_ref)
            result = {"status": "fresh", **wire, "event": event, "outbox_message_ref": message_id, "outbox_event": outbox_event}
            self._save_idem(cur, idempotency_key, "decide_agenda_cycle", request_hash, result)
            return result

    def _outbox_event(
        self,
        cur: sqlite3.Cursor,
        message_id: str,
        state: str,
        *,
        actor_ref: str,
        delivery_attempt_id: str | None = None,
        claim_expires_at: str | None = None,
        endpoint_ref: str | None = None,
        retry_after: str | None = None,
        delivery_receipt_id: str | None = None,
        error_code: str | None = None,
    ) -> dict[str, Any]:
        created_at = _now()
        wire = self._record({
            "schema_version": SCHEMA_VERSION,
            "id": f"agenda-outbox-event:{uuid.uuid4().hex}",
            "message_ref": message_id,
            "state": state,
            "delivery_attempt_id": delivery_attempt_id,
            "claim_expires_at": claim_expires_at,
            "endpoint_ref": endpoint_ref,
            "retry_after": retry_after,
            "delivery_receipt_id": delivery_receipt_id,
            "error_code": error_code,
            "actor_ref": _text(actor_ref, "actor_ref"),
            "created_at": created_at,
        })
        cur.execute(
            "INSERT INTO agenda_outbox_events("
            "event_id,message_id,state,delivery_attempt_id,claim_expires_at,endpoint_ref,"
            "retry_after,delivery_receipt_id,error_code,actor_ref,created_at,content_hash"
            ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                wire["id"], message_id, state, delivery_attempt_id, claim_expires_at,
                endpoint_ref, retry_after, delivery_receipt_id, error_code,
                wire["actor_ref"], created_at, wire["content_hash"],
            ),
        )
        return wire

    @staticmethod
    def _outbox_wire(row: sqlite3.Row) -> dict[str, Any]:
        return {**dict(row), "payload": json.loads(row["payload_json"])}

    def claim_outbox(
        self,
        *,
        endpoint_ref: str,
        actor_ref: str,
        idempotency_key: str,
        now: str | None = None,
        claim_ttl_seconds: int = 120,
        max_attempts: int = 5,
        limit: int = 1,
    ) -> dict[str, Any]:
        endpoint_ref = _text(endpoint_ref, "endpoint_ref")
        actor_ref = _text(actor_ref, "actor_ref")
        now = _timestamp(now or _now(), "now")
        for value, name, upper in (
            (claim_ttl_seconds, "claim_ttl_seconds", 3600),
            (max_attempts, "max_attempts", 100),
            (limit, "limit", 100),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= upper:
                raise AgendaValidationError(f"{name} must be 1..{upper}")
        request = {
            "endpoint_ref": endpoint_ref,
            "actor_ref": actor_ref,
            "now": now,
            "claim_ttl_seconds": claim_ttl_seconds,
            "max_attempts": max_attempts,
            "limit": limit,
        }
        request_hash = content_hash(request)
        with self.store._transaction() as cur:
            duplicate = self._idem(cur, idempotency_key, "claim_agenda_outbox", request_hash)
            if duplicate is not None:
                return duplicate
            rows = cur.execute(
                "WITH latest AS ("
                " SELECT e.* FROM agenda_outbox_events e"
                " JOIN (SELECT message_id,MAX(event_seq) AS event_seq FROM agenda_outbox_events GROUP BY message_id) x"
                " ON x.event_seq=e.event_seq"
                "), attempts AS ("
                " SELECT message_id,COUNT(*) AS attempt_count FROM agenda_outbox_events"
                " WHERE state='claimed' GROUP BY message_id"
                ")"
                " SELECT m.*,l.state,l.delivery_attempt_id,l.claim_expires_at,l.retry_after,"
                " COALESCE(a.attempt_count,0) AS attempt_count"
                " FROM agenda_outbox_messages m JOIN latest l ON l.message_id=m.message_id"
                " LEFT JOIN attempts a ON a.message_id=m.message_id"
                " WHERE COALESCE(a.attempt_count,0)<? AND ("
                " l.state='pending' OR"
                " (l.state='failed' AND (l.retry_after IS NULL OR l.retry_after<=?)) OR"
                " (l.state='claimed' AND l.claim_expires_at IS NOT NULL AND l.claim_expires_at<=?)"
                ") ORDER BY m.created_at,m.message_id LIMIT ?",
                (max_attempts, now, now, limit),
            ).fetchall()
            claims: list[dict[str, Any]] = []
            expires_at = (
                datetime.fromisoformat(str(now)).astimezone(timezone.utc)
                + timedelta(seconds=claim_ttl_seconds)
            ).isoformat(timespec="microseconds")
            for row in rows:
                attempt_number = int(row["attempt_count"]) + 1
                attempt_id = (
                    "agenda-delivery-attempt:"
                    + content_hash({
                        "message_id": row["message_id"],
                        "attempt_number": attempt_number,
                        "endpoint_ref": endpoint_ref,
                    })[:32]
                )
                event = self._outbox_event(
                    cur,
                    row["message_id"],
                    "claimed",
                    actor_ref=actor_ref,
                    delivery_attempt_id=attempt_id,
                    claim_expires_at=expires_at,
                    endpoint_ref=endpoint_ref,
                )
                claims.append({
                    **self._outbox_wire(row),
                    "attempt_number": attempt_number,
                    "delivery_attempt_id": attempt_id,
                    "claim_expires_at": expires_at,
                    "endpoint_ref": endpoint_ref,
                    "claim_event": event,
                })
            result = {"status": "fresh", "claims": claims}
            self._save_idem(cur, idempotency_key, "claim_agenda_outbox", request_hash, result)
            return result

    def pending_outbox(self, *, limit: int = 100) -> list[dict[str, Any]]:
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise AgendaValidationError("limit must be 1..1000")
        rows = self.connection.execute(
            "SELECT m.*,e.state,e.delivery_attempt_id,e.claim_expires_at,e.endpoint_ref,"
            "e.retry_after,e.delivery_receipt_id,e.error_code "
            "FROM agenda_outbox_messages m JOIN agenda_outbox_events e "
            "ON e.event_seq=(SELECT MAX(x.event_seq) FROM agenda_outbox_events x WHERE x.message_id=m.message_id) "
            "WHERE e.state IN ('pending','claimed','failed') ORDER BY m.created_at LIMIT ?",
            (limit,),
        ).fetchall()
        return [self._outbox_wire(row) for row in rows]

    def record_delivery(
        self,
        message_id: str,
        *,
        state: str,
        delivery_attempt_id: str,
        actor_ref: str,
        idempotency_key: str,
        delivery_receipt_id: str | None = None,
        error_code: str | None = None,
        retry_after: str | None = None,
    ) -> dict[str, Any]:
        message_id = _text(message_id, "message_id")
        delivery_attempt_id = _text(delivery_attempt_id, "delivery_attempt_id")
        if state not in {"delivered", "failed"}:
            raise AgendaValidationError("delivery completion state is invalid")
        if state == "delivered" and not delivery_receipt_id:
            raise AgendaValidationError("delivered outbox message requires a receipt id")
        if state == "failed" and not error_code:
            raise AgendaValidationError("failed outbox message requires an error code")
        if state == "failed":
            retry_after = _timestamp(retry_after, "retry_after")
        elif retry_after is not None:
            raise AgendaValidationError("delivered outbox message cannot set retry_after")
        request = {
            "message_id": message_id,
            "state": state,
            "delivery_attempt_id": delivery_attempt_id,
            "delivery_receipt_id": delivery_receipt_id,
            "error_code": error_code,
            "retry_after": retry_after,
            "actor_ref": _text(actor_ref, "actor_ref"),
        }
        request_hash = content_hash(request)
        with self.store._transaction() as cur:
            duplicate = self._idem(cur, idempotency_key, "record_agenda_delivery", request_hash)
            if duplicate is not None:
                return duplicate
            if cur.execute("SELECT 1 FROM agenda_outbox_messages WHERE message_id=?", (message_id,)).fetchone() is None:
                raise AgendaNotFound(message_id)
            latest = cur.execute(
                "SELECT * FROM agenda_outbox_events WHERE message_id=? ORDER BY event_seq DESC LIMIT 1",
                (message_id,),
            ).fetchone()
            if latest is None or latest["state"] != "claimed":
                raise AgendaConflict("outbox message is not claimed")
            if latest["delivery_attempt_id"] != delivery_attempt_id:
                raise AgendaConflict("delivery attempt is stale")
            event = self._outbox_event(
                cur,
                message_id,
                state,
                actor_ref=request["actor_ref"],
                delivery_attempt_id=delivery_attempt_id,
                endpoint_ref=latest["endpoint_ref"],
                retry_after=retry_after,
                delivery_receipt_id=delivery_receipt_id,
                error_code=error_code,
            )
            if state == "delivered":
                cycle = cur.execute(
                    "SELECT d.cycle_id FROM agenda_outbox_messages m "
                    "JOIN agenda_decisions d ON json_extract(m.payload_json,'$.decision_ref')=d.decision_id "
                    "WHERE m.message_id=?",
                    (message_id,),
                ).fetchone()
                if cycle is not None and self._latest_cycle_state(cur, cycle["cycle_id"]) == "decided":
                    self._cycle_event(
                        cur,
                        cycle["cycle_id"],
                        "delivered",
                        "agenda_card_delivered",
                        {"message_ref": message_id},
                        request["actor_ref"],
                    )
            self._event(
                cur,
                f"agenda_delivery_{state}",
                message_id,
                {"delivery_attempt_ref": delivery_attempt_id, "receipt_ref": delivery_receipt_id},
                request["actor_ref"],
            )
            result = {"status": "fresh", **event}
            self._save_idem(cur, idempotency_key, "record_agenda_delivery", request_hash, result)
            return result

    def record_feedback(
        self,
        decision_id: str,
        *,
        verdict: str,
        notes: str,
        actor_ref: str,
        feedback_id: str | None = None,
        idempotency_key: str | None = None,
        subject_ref: str | None = None,
        prior_feedback_ref: str | None = None,
        source: str = "local_cli",
        source_event_ref: str | None = None,
    ) -> dict[str, Any]:
        decision_id = _text(decision_id, "decision_id")
        if verdict not in {"agree", "disagree", "partial"}:
            raise AgendaValidationError("feedback verdict is invalid")
        notes = notes if isinstance(notes, str) else ""
        actor_ref = _text(actor_ref, "actor_ref")
        subject_ref = _text(subject_ref or actor_ref, "subject_ref")
        source = _text(source, "source")
        if source not in {
            "local_cli", "openclaw_discord_reaction", "tailscale_dashboard",
            "auto_accept_timeout",
        }:
            raise AgendaValidationError("feedback source is invalid")
        source_event_ref = None if source_event_ref is None else _text(source_event_ref, "source_event_ref")
        prior_feedback_ref = None if prior_feedback_ref is None else _text(prior_feedback_ref, "prior_feedback_ref")
        feedback_id = self._id("agenda-feedback", feedback_id)
        request = {
            "decision_id": decision_id, "verdict": verdict, "notes": notes,
            "actor_ref": actor_ref, "subject_ref": subject_ref,
            "prior_feedback_ref": prior_feedback_ref, "source": source,
            "source_event_ref": source_event_ref, "feedback_id": feedback_id,
        }
        request_hash = content_hash(request)
        with self.store._transaction() as cur:
            duplicate = self._idem(cur, idempotency_key, "record_agenda_feedback", request_hash)
            if duplicate is not None:
                return duplicate
            if cur.execute("SELECT 1 FROM agenda_decisions WHERE decision_id=?", (decision_id,)).fetchone() is None:
                raise AgendaNotFound(decision_id)
            latest = cur.execute(
                "SELECT * FROM agenda_feedback WHERE decision_id=? AND COALESCE(subject_ref,actor_ref)=? "
                "ORDER BY created_at DESC,feedback_id DESC LIMIT 1",
                (decision_id, subject_ref),
            ).fetchone()
            latest_ref = None if latest is None else latest["feedback_id"]
            if latest_ref != prior_feedback_ref:
                raise AgendaConflict("feedback prior version is stale")
            if latest is not None and latest["verdict"] == verdict:
                return {"status": "duplicate", **dict(latest)}
            created_at = _now()
            wire = self._record({
                "schema_version": SCHEMA_VERSION, "id": feedback_id,
                "decision_ref": decision_id, "prior_feedback_ref": prior_feedback_ref,
                "subject_ref": subject_ref, "verdict": verdict, "notes": notes,
                "source": source, "source_event_ref": source_event_ref,
                "actor_ref": actor_ref, "created_at": created_at,
            })
            cur.execute(
                "INSERT INTO agenda_feedback(feedback_id,decision_id,prior_feedback_id,subject_ref,"
                "verdict,notes,source,source_event_ref,actor_ref,created_at,content_hash) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    feedback_id, decision_id, prior_feedback_ref, subject_ref, verdict, notes,
                    source, source_event_ref, actor_ref, created_at, wire["content_hash"],
                ),
            )
            self._event(cur, "agenda_feedback_recorded", decision_id, {"feedback_ref": feedback_id, "subject_ref": subject_ref, "verdict": verdict}, actor_ref)
            result = {"status": "fresh", **wire}
            self._save_idem(cur, idempotency_key, "record_agenda_feedback", request_hash, result)
            return result

    def feedback_targets(self, *, endpoint_ref: str, limit: int = 100) -> list[dict[str, Any]]:
        endpoint_ref = _text(endpoint_ref, "endpoint_ref")
        if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
            raise AgendaValidationError("limit must be 1..1000")
        rows = self.connection.execute(
            "WITH latest_delivery AS ("
            " SELECT e.* FROM agenda_outbox_events e JOIN ("
            "  SELECT message_id,MAX(event_seq) AS event_seq FROM agenda_outbox_events GROUP BY message_id"
            " ) x ON x.event_seq=e.event_seq"
            "), latest_feedback AS ("
            " SELECT f.* FROM agenda_feedback f JOIN ("
            "  SELECT decision_id,COALESCE(subject_ref,actor_ref) AS subject_ref,MAX(created_at) AS created_at"
            "  FROM agenda_feedback GROUP BY decision_id,COALESCE(subject_ref,actor_ref)"
            " ) x ON x.decision_id=f.decision_id AND x.subject_ref=COALESCE(f.subject_ref,f.actor_ref) AND x.created_at=f.created_at"
            ")"
            " SELECT m.message_id,m.payload_json,m.payload_hash,d.delivery_receipt_id,d.endpoint_ref,"
            " d.created_at AS delivered_at,f.feedback_id,f.subject_ref,f.verdict,f.source,"
            " f.created_at AS feedback_created_at"
            " FROM agenda_outbox_messages m JOIN latest_delivery d ON d.message_id=m.message_id"
            " LEFT JOIN latest_feedback f ON f.decision_id=json_extract(m.payload_json,'$.decision_ref')"
            " WHERE d.state='delivered' AND d.endpoint_ref=?"
            " ORDER BY m.created_at DESC,f.subject_ref LIMIT ?",
            (endpoint_ref, limit),
        ).fetchall()
        grouped: dict[str, dict[str, Any]] = {}
        for row in rows:
            item = grouped.setdefault(
                row["message_id"],
                {
                    "message_id": row["message_id"],
                    "payload": json.loads(row["payload_json"]),
                    "payload_hash": row["payload_hash"],
                    "delivery_receipt_id": row["delivery_receipt_id"],
                    "endpoint_ref": row["endpoint_ref"],
                    "delivered_at": row["delivered_at"],
                    "latest_feedback": {},
                },
            )
            if row["subject_ref"]:
                item["latest_feedback"][row["subject_ref"]] = {
                    "feedback_id": row["feedback_id"], "verdict": row["verdict"],
                    "source": row["source"], "created_at": row["feedback_created_at"],
                }
        return list(grouped.values())

    def cycle(self, cycle_id: str) -> dict[str, Any]:
        cycle_id = _text(cycle_id, "cycle_id")
        row = self.connection.execute("SELECT * FROM agenda_cycles WHERE cycle_id=?", (cycle_id,)).fetchone()
        if row is None:
            raise AgendaNotFound(cycle_id)
        events = self.connection.execute("SELECT * FROM agenda_cycle_events WHERE cycle_id=? ORDER BY event_seq", (cycle_id,)).fetchall()
        candidates = self.connection.execute("SELECT * FROM agenda_candidates WHERE cycle_id=? ORDER BY candidate_id", (cycle_id,)).fetchall()
        decision = self.connection.execute("SELECT * FROM agenda_decisions WHERE cycle_id=?", (cycle_id,)).fetchone()
        return {
            "cycle": dict(row),
            "state": events[-1]["state"],
            "events": [dict(item) for item in events],
            "candidates": [dict(item) for item in candidates],
            "decision": None if decision is None else dict(decision),
        }

    def cycle_by_key(self, cycle_key: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT cycle_id FROM agenda_cycles WHERE cycle_key=?",
            (_text(cycle_key, "cycle_key"),),
        ).fetchone()
        return None if row is None else self.cycle(row["cycle_id"])


__all__ = [
    "AgendaStore", "AgendaError", "AgendaValidationError", "AgendaConflict",
    "AgendaNotFound", "AgendaPaused", "FEATURE_NAMES", "validate_features",
    "validate_policy",
]
