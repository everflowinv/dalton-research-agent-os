"""SQLite hybrid-temporal transaction kernel for Dalton Core."""

from __future__ import annotations

import dataclasses
import hashlib
import json
import os
import sqlite3
import uuid
from collections.abc import Mapping
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterator
from .contracts import (
    AdjudicatedStatus,
    AdjudicationVersion,
    ClaimKind,
    ClaimVersion,
    EvidenceRelation,
    EvidenceRelationType,
    EvidenceVersion,
    ExecutionInvocation,
    ExecutionKind,
    GovernancePolicyVersion,
    ModelInvocation,
    ThesisVersion,
    VerificationRecord,
    Verdict,
)

from .errors import (
    BadVerdict,
    GateRejected,
    IdempotencyConflict,
    InvocationConflict,
    IndependenceViolation,
    NotFound,
    ValidationError,
    VerificationRequired,
)
from .policy import DEFAULT_POLICY, canonical_policy, evaluate_gate


_SCHEMA_PATH = Path(__file__).with_name("schema.sql")
_THESIS_FIELDS = frozenset({"statement", "mechanism", "confidence", "implied_expectation", "claim_refs", "catalyst_refs", "falsifier_refs", "change_reason"})
_THESIS_CONFIDENCE_LEVELS = frozenset({"low", "medium", "high"})
_CLAIM_FIELDS = frozenset({"subject_ref", "metric_or_aspect", "period", "basis", "normalized_statement", "claim_kind", "value", "unit", "producer_invocation_refs", "actor_ref"})


def canonical_json(value: Any) -> str:
    """Serialize JSON deterministically for hashes and durable payloads."""
    if dataclasses.is_dataclass(value):
        value = dataclasses.asdict(value)
    if isinstance(value, Mapping):
        value = {str(k): v for k, v in value.items()}
        value = {k: json.loads(canonical_json(v)) for k, v in value.items()}
    elif isinstance(value, (tuple, list)):
        value = [json.loads(canonical_json(v)) for v in value]
    elif isinstance(value, set):
        value = sorted(value)
    elif hasattr(value, "value"):
        value = value.value
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def content_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _id(value: Any = None) -> str:
    return str(value) if value is not None else uuid.uuid4().hex


def _mapping(value: Any) -> dict[str, Any]:
    if value is None:
        return {}
    if dataclasses.is_dataclass(value):
        return dataclasses.asdict(value)
    if isinstance(value, Mapping):
        return dict(value)
    raise ValidationError("expected a mapping or dataclass")


def _scalar(value: Any) -> Any:
    """Turn Enum-like contract values into their wire value."""
    return getattr(value, "value", value)


def _parse_rfc3339(value: str, name: str) -> datetime:
    if not isinstance(value, str):
        raise ValidationError(f"{name} must be RFC3339")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValidationError(f"{name} must be RFC3339") from exc
    if parsed.tzinfo is None:
        raise ValidationError(f"{name} must include timezone")
    return parsed.astimezone(timezone.utc)


class DaltonStore:
    """Owns all writes to the hybrid-temporal SQLite database.

    ``stage_change``, ``verify_change`` and ``commit`` each open and commit a
    separate SQLite transaction.  The connection is intentionally exposed as
    ``connection`` for read-only projections and for tests of the trigger
    boundary; direct writes to authoritative tables are rejected by schema
    triggers.
    """

    def __init__(self, path: str | Path = ":memory:", *, connection: sqlite3.Connection | None = None):
        self.path = str(path)
        self._authorized = False
        self.connection = connection or sqlite3.connect(self.path, isolation_level=None)
        if connection is None and self.path != ":memory:":
            os.chmod(self.path, 0o600)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA busy_timeout = 5000")
        self.connection.create_function("dalton_authorized", 0, lambda: int(self._authorized))
        self.connection.executescript(_SCHEMA_PATH.read_text(encoding="utf-8"))
        self._migrate_thesis_authority_columns()
        self._backfill_model_execution_links()
        self._ensure_default_policy()

    def _migrate_thesis_authority_columns(self) -> None:
        """Upgrade the legacy model-verification-only thesis table in place.

        Existing v0.1 rows keep their exact JSON and verification binding.  The
        additive authority columns permit v0.2 rows admitted by an explicit
        human decision without inventing a model invocation.  Referencing
        tables continue to point at the same ``thesis_versions`` name and ids.
        """

        columns = {
            row[1] for row in self.connection.execute(
                "PRAGMA table_info(thesis_versions)"
            ).fetchall()
        }
        if {"admission_decision_id", "authority_kind", "authority_ref"} <= columns:
            return
        legacy = {
            "version_id", "thesis_id", "version_number", "content_json",
            "content_hash", "prior_version_id", "change_id", "verification_id",
            "committed_by", "created_at",
        }
        if columns != legacy:
            raise ValidationError("thesis_versions has an unsupported legacy shape")
        if self.connection.in_transaction:
            raise RuntimeError("thesis authority migration requires no open transaction")
        self.connection.execute("PRAGMA foreign_keys = OFF")
        try:
            self.connection.executescript(
                """
                BEGIN IMMEDIATE;
                DROP TRIGGER IF EXISTS thesis_versions_no_update;
                DROP TRIGGER IF EXISTS thesis_versions_no_delete;
                DROP TRIGGER IF EXISTS thesis_versions_authorized_insert;
                CREATE TABLE thesis_versions_v2 (
                    version_id TEXT PRIMARY KEY,
                    thesis_id TEXT NOT NULL,
                    version_number INTEGER NOT NULL,
                    content_json TEXT NOT NULL,
                    content_hash TEXT NOT NULL,
                    prior_version_id TEXT REFERENCES thesis_versions_v2(version_id),
                    change_id TEXT REFERENCES staging_changes(change_id),
                    verification_id TEXT REFERENCES verification_records(verification_id),
                    admission_decision_id TEXT REFERENCES thesis_admission_decisions(decision_id),
                    authority_kind TEXT NOT NULL CHECK(authority_kind IN ('verification','human_admission')),
                    authority_ref TEXT NOT NULL,
                    committed_by TEXT,
                    created_at TEXT NOT NULL,
                    CHECK(
                        (authority_kind='verification' AND change_id IS NOT NULL AND verification_id IS NOT NULL AND admission_decision_id IS NULL AND authority_ref=verification_id)
                        OR
                        (authority_kind='human_admission' AND change_id IS NULL AND verification_id IS NULL AND admission_decision_id IS NOT NULL AND authority_ref=admission_decision_id)
                    ),
                    UNIQUE (thesis_id, version_number)
                );
                INSERT INTO thesis_versions_v2(
                    version_id,thesis_id,version_number,content_json,content_hash,
                    prior_version_id,change_id,verification_id,admission_decision_id,
                    authority_kind,authority_ref,committed_by,created_at
                )
                SELECT version_id,thesis_id,version_number,content_json,content_hash,
                       prior_version_id,change_id,verification_id,NULL,
                       'verification',verification_id,committed_by,created_at
                FROM thesis_versions;
                DROP TABLE thesis_versions;
                ALTER TABLE thesis_versions_v2 RENAME TO thesis_versions;
                CREATE TRIGGER thesis_versions_no_update
                BEFORE UPDATE ON thesis_versions BEGIN
                    SELECT RAISE(ABORT, 'thesis_versions is immutable');
                END;
                CREATE TRIGGER thesis_versions_no_delete
                BEFORE DELETE ON thesis_versions BEGIN
                    SELECT RAISE(ABORT, 'thesis_versions is immutable');
                END;
                CREATE TRIGGER thesis_versions_authorized_insert
                BEFORE INSERT ON thesis_versions
                WHEN dalton_authorized() = 0 BEGIN
                    SELECT RAISE(ABORT, 'thesis_versions insert requires DaltonStore');
                END;
                COMMIT;
                """
            )
        finally:
            self.connection.execute("PRAGMA foreign_keys = ON")
        violations = self.connection.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise ValidationError("thesis authority migration broke foreign keys")

    @property
    def conn(self) -> sqlite3.Connection:
        return self.connection

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "DaltonStore":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Cursor]:
        if self.connection.in_transaction:
            raise RuntimeError("DaltonStore operation cannot be nested in an open transaction")
        self.connection.execute("BEGIN IMMEDIATE")
        self._authorized = True
        try:
            yield self.connection.cursor()
            self.connection.commit()
        except BaseException:
            self.connection.rollback()
            raise
        finally:
            self._authorized = False

    def _ensure_default_policy(self) -> None:
        row = self.connection.execute("SELECT 1 FROM governance_policy_pointer WHERE pointer_id=1").fetchone()
        if row:
            return
        self.create_policy(DEFAULT_POLICY, policy_version_id="policy-1", version_number=1, activate=True)

    @staticmethod
    def _assert_policy_effective(policy_row: sqlite3.Row | Mapping[str, Any]) -> None:
        """Reject future or expired active policy pointers consistently."""
        effective_from = policy_row["effective_from"]
        effective_until = policy_row["effective_until"]
        now_dt = datetime.now(timezone.utc)
        start_dt = _parse_rfc3339(effective_from, "effective_from")
        end_dt = _parse_rfc3339(effective_until, "effective_until") if effective_until is not None else None
        if now_dt < start_dt or (end_dt is not None and now_dt >= end_dt):
            raise GateRejected("active governance policy is outside its effective interval")

    @staticmethod
    def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
        return dict(row) if row is not None else None

    def _ensure_execution_invocation(
        self, cur: sqlite3.Cursor, execution: ExecutionInvocation | Mapping[str, Any]
    ) -> dict[str, Any]:
        try:
            validated = (
                execution
                if isinstance(execution, ExecutionInvocation)
                else ExecutionInvocation.from_dict(execution)
            )
        except Exception as exc:
            raise ValidationError(str(exc)) from exc
        wire = validated.to_dict()
        digest = content_hash(wire)
        row = cur.execute(
            "SELECT execution_json,content_hash,kind FROM execution_invocations WHERE execution_id=?",
            (validated.id,),
        ).fetchone()
        if row is not None:
            if row["content_hash"] != digest or row["execution_json"] != canonical_json(wire):
                raise InvocationConflict(
                    f"execution_id {validated.id!r} already has a different canonical payload"
                )
            return {
                "execution_id": validated.id,
                "kind": row["kind"],
                "execution_json": row["execution_json"],
                "content_hash": row["content_hash"],
            }
        cur.execute(
            "INSERT INTO execution_invocations"
            "(execution_id,kind,work_order_ref,profile_ref,capability,runtime_ref,actor_ref,"
            "parent_ref,environment_hash,execution_json,content_hash,created_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                validated.id,
                validated.kind.value,
                validated.work_order_ref,
                validated.profile_ref,
                validated.capability,
                validated.runtime_ref,
                validated.actor_ref,
                validated.parent_ref,
                validated.environment_hash,
                canonical_json(wire),
                digest,
                _now(),
            ),
        )
        return {
            "execution_id": validated.id,
            "kind": validated.kind.value,
            "execution_json": canonical_json(wire),
            "content_hash": digest,
        }

    def _ensure_model_execution_link(
        self, cur: sqlite3.Cursor, invocation: ModelInvocation
    ) -> dict[str, Any]:
        execution = ExecutionInvocation.from_model(invocation)
        execution_row = self._ensure_execution_invocation(cur, execution)
        link_wire = {
            "execution_ref": execution.id,
            "model_invocation_ref": invocation.id,
        }
        link_hash = content_hash(link_wire)
        row = cur.execute(
            "SELECT * FROM execution_invocation_model_links WHERE execution_ref=?",
            (execution.id,),
        ).fetchone()
        if row is not None:
            if row["model_invocation_ref"] != invocation.id or row["content_hash"] != link_hash:
                raise InvocationConflict("execution/model subtype link conflicts with immutable data")
            return execution_row
        cur.execute(
            "INSERT INTO execution_invocation_model_links"
            "(execution_ref,model_invocation_ref,content_hash,created_at) VALUES(?,?,?,?)",
            (execution.id, invocation.id, link_hash, _now()),
        )
        return execution_row

    @staticmethod
    def _model_invocation_from_saved(value: str | Mapping[str, Any]) -> ModelInvocation:
        raw = json.loads(value) if isinstance(value, str) else dict(value)
        raw.pop("invocation_id", None)
        return ModelInvocation.from_dict(raw)

    def _backfill_model_execution_links(self) -> None:
        rows = self.connection.execute(
            "SELECT invocation_json FROM model_invocations "
            "WHERE invocation_id NOT IN (SELECT model_invocation_ref FROM execution_invocation_model_links) "
            "ORDER BY invocation_id"
        ).fetchall()
        if not rows:
            return
        with self._transaction() as cur:
            for row in rows:
                try:
                    invocation = self._model_invocation_from_saved(row["invocation_json"])
                except Exception as exc:
                    raise ValidationError("legacy model invocation cannot be backfilled") from exc
                self._ensure_model_execution_link(cur, invocation)

    def _ensure_invocation(self, cur: sqlite3.Cursor, invocation: Any, *, required_id: str | None = None) -> dict[str, Any]:
        data = _mapping(invocation)
        if dataclasses.is_dataclass(invocation) and hasattr(invocation, "to_dict"):
            data = dict(invocation.to_dict())
        if "id" in data and "invocation_id" in data:
            raise ValidationError("id and invocation_id cannot both be supplied")
        if "id" in data and "invocation_id" not in data:
            data["invocation_id"] = data["id"]
        invocation_id = _id(data.get("invocation_id", data.get("id", required_id)))
        if required_id and invocation_id != required_id:
            raise ValidationError("invocation_id does not match the required reference")
        data["invocation_id"] = invocation_id
        row = cur.execute("SELECT * FROM model_invocations WHERE invocation_id=?", (invocation_id,)).fetchone()
        if row:
            saved = json.loads(row["invocation_json"])
            self._ensure_model_execution_link(cur, self._model_invocation_from_saved(saved))
            # A repeated reference with no metadata is a read-only reference.
            # Once a payload is supplied, normalize aliases and compare the
            # complete canonical payload; an id can never identify two facts.
            supplied = {k: v for k, v in data.items() if k not in {"invocation_id", "id"} and v is not None}
            if supplied:
                normalized_saved = self._normalized_invocation_payload(saved)
                normalized_new = self._normalized_invocation_payload(data)
                if canonical_json(normalized_saved) != canonical_json(normalized_new):
                    raise InvocationConflict(f"invocation_id {invocation_id!r} already has a different canonical payload")
            return dict(row) | {"invocation_id": invocation_id}
        # A new authoritative invocation must carry contract provenance.  A
        # reference containing only an id is valid only when the id already
        # exists (handled above).
        required = ("schema_version", "created_at", "work_order_ref", "profile_ref", "granularity", "capability", "provider", "model", "runtime_ref", "actor_ref", "usage", "started_at")
        aliases = {}
        missing = []
        for field in required:
            names = aliases.get(field, (field,))
            if not any(data.get(name) is not None for name in names):
                missing.append(field)
        if data.get("model_family") is None:
            missing.append("model_family")
        if missing:
            raise ValidationError(f"new model invocation is missing required fields: {missing}")
        if not isinstance(data.get("usage"), Mapping):
            raise ValidationError("model invocation usage must be a mapping")
        data.setdefault("input_refs", [])
        data.setdefault("output_refs", [])
        data.setdefault("side_effects", [])
        data.setdefault("completed_at", None)
        data.setdefault("parent_ref", None)
        data.setdefault("environment_hash", None)
        try:
            contract_data = dict(data)
            contract_data.pop("invocation_id", None)
            validated = ModelInvocation.from_dict(contract_data)
        except Exception as exc:
            raise ValidationError(str(exc)) from exc
        data = validated.to_dict()
        data["invocation_id"] = validated.id
        self._ensure_execution_invocation(cur, ExecutionInvocation.from_model(validated))
        created = _now()
        family = data.get("model_family")
        profile = data.get("profile_ref")
        provider = data.get("provider")
        model = data.get("model")
        capability = data.get("capability")
        runtime_ref = data.get("runtime_ref")
        actor_ref = data.get("actor_ref")
        environment_hash = data.get("environment_hash")
        granularity = _scalar(data.get("granularity"))
        work_order_ref = data.get("work_order_ref")
        role = None
        cur.execute(
            "INSERT INTO model_invocations(invocation_id,profile_ref,provider,model,capability,runtime_ref,actor_ref,environment_hash,granularity,work_order_ref,model_family,invocation_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (invocation_id, profile, provider, model, capability, runtime_ref, actor_ref, environment_hash, granularity, work_order_ref, family, canonical_json(data), created),
        )
        self._ensure_model_execution_link(cur, validated)
        return {
            "invocation_id": invocation_id,
            "model_family": family,
            "profile_ref": profile,
            "provider": provider,
            "model": model,
            "capability": capability,
            "runtime_ref": runtime_ref,
            "actor_ref": actor_ref,
            "environment_hash": environment_hash,
            "granularity": granularity,
            "invocation_json": canonical_json(data),
            "created_at": created,
        }

    @staticmethod
    def _normalized_invocation_payload(data: Mapping[str, Any]) -> dict[str, Any]:
        """Normalize contract aliases while retaining complete usage metadata."""
        result = dict(data)
        result.pop("id", None)
        result.pop("invocation_id", None)
        aliases = {
            "profile_id": "profile_ref",
            "provider_model_id": "provider",
            "model_id": "model",
        }
        for old, new in aliases.items():
            if old in result and new not in result:
                result[new] = result[old]
            result.pop(old, None)
        return result

    def register_invocation(self, invocation: Any = None, **fields: Any) -> dict[str, Any]:
        data = _mapping(invocation)
        data.update(fields)
        if not data.get("invocation_id", data.get("id")):
            raise ValidationError("invocation_id is required")
        with self._transaction() as cur:
            return self._ensure_invocation(cur, data)

    add_model_invocation = register_invocation

    def create_policy(
        self,
        policy: Mapping[str, Any] | Any,
        *,
        policy_version_id: str | None = None,
        version_number: int | None = None,
        activate: bool = True,
        policy_ref: str | None = None,
        effective_from: str | None = None,
        effective_until: str | None = None,
        actor_ref: str | None = None,
        prior_version_ref: str | None = None,
        change_reason: str | None = None,
        content_hash_value: str | None = None,
    ) -> dict[str, Any]:
        raw_policy = _mapping(policy)
        # GovernancePolicyVersion carries the executable policy under
        # ``policy`` and predicate records beside it; accept that dataclass
        # shape without making contracts.py a dependency of the store.
        metadata = raw_policy
        if isinstance(raw_policy.get("policy"), Mapping):
            nested = dict(raw_policy["policy"])
            if "independence_predicates" not in nested and raw_policy.get("independence_predicates"):
                nested["independence_predicates"] = raw_policy["independence_predicates"]
            raw_policy = nested
        try:
            p = canonical_policy(raw_policy)
        except ValueError as exc:
            raise ValidationError(str(exc)) from exc
        if not isinstance(p.get("allowed_verdicts"), (list, tuple, set)):
            raise ValidationError("allowed_verdicts must be a sequence")
        pid = _id(policy_version_id or metadata.get("id"))
        with self._transaction() as cur:
            policy_ref = policy_ref or metadata.get("policy_ref", "commit-gate")
            if version_number is None:
                version_number = int(metadata.get("version", 0)) or int(cur.execute(
                    "SELECT COALESCE(MAX(version_number),0)+1 FROM governance_policy_versions WHERE policy_ref=?",
                    (policy_ref,),
                ).fetchone()[0])
            prior = prior_version_ref or metadata.get("prior_version_ref")
            if version_number == 1:
                if prior:
                    raise ValidationError("policy version 1 cannot have a prior version")
            elif not prior:
                prior_row = cur.execute("SELECT policy_version_id FROM governance_policy_versions WHERE version_number=? AND policy_ref=?", (version_number - 1, policy_ref)).fetchone()
                if not prior_row:
                    raise ValidationError("policy version chain has no immediately prior version")
                prior = prior_row[0]
            else:
                prior_row = cur.execute("SELECT policy_version_id, policy_ref, version_number FROM governance_policy_versions WHERE policy_version_id=?", (prior,)).fetchone()
                if not prior_row or prior_row[1] != policy_ref or prior_row[2] != version_number - 1:
                    raise ValidationError("prior policy version must exist, share policy_ref, and be version N-1")
            effective_from = effective_from or metadata.get("effective_from") or _now()
            effective_until = effective_until if effective_until is not None else metadata.get("effective_until")
            actor_ref = actor_ref or metadata.get("actor_ref", "system:dalton-core")
            change_reason = change_reason or metadata.get("change_reason", "policy update")
            from_dt = _parse_rfc3339(effective_from, "effective_from")
            until_dt = _parse_rfc3339(effective_until, "effective_until") if effective_until is not None else None
            if until_dt is not None and from_dt >= until_dt:
                raise ValidationError("effective_from must be before effective_until")
            created = metadata.get("created_at") or _now()
            predicates = []
            for pred in p.get("independence_predicates", []):
                pred = dict(pred)
                predicates.append({"left_path": pred["left_path"], "operator": pred["operator"], **({"right_path": pred["right_path"]} if "right_path" in pred else {"value": pred.get("value")})})
            wire_base = {
                "schema_version": "0.1", "id": pid, "created_at": created,
                "policy_ref": policy_ref, "version": version_number,
                "effective_from": effective_from, "effective_until": effective_until,
                "policy": {k: v for k, v in p.items() if k != "independence_predicates"},
                "independence_predicates": predicates, "change_reason": change_reason,
                "actor_ref": actor_ref, "prior_version_ref": prior,
            }
            computed_policy_hash = content_hash(wire_base)
            supplied_hash = content_hash_value or metadata.get("content_hash")
            if supplied_hash is not None and supplied_hash != computed_policy_hash:
                raise ValidationError("policy content_hash does not match canonical policy version")
            version_wire = dict(wire_base, content_hash=computed_policy_hash)
            try:
                validated_policy = GovernancePolicyVersion.from_dict(version_wire)
            except Exception as exc:
                raise ValidationError(str(exc)) from exc
            version_encoded = canonical_json(validated_policy.to_dict())
            encoded = canonical_json(p)
            cur.execute(
                "INSERT INTO governance_policy_versions(policy_version_id,version_number,policy_ref,effective_from,effective_until,actor_ref,prior_version_ref,change_reason,version_json,policy_json,content_hash,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                (pid, version_number, policy_ref, effective_from, effective_until, actor_ref, prior, change_reason, version_encoded, encoded, computed_policy_hash, created),
            )
            if activate:
                cur.execute(
                    "INSERT INTO governance_policy_pointer(pointer_id,policy_version_id,updated_at) VALUES(1,?,?) "
                    "ON CONFLICT(pointer_id) DO UPDATE SET policy_version_id=excluded.policy_version_id, updated_at=excluded.updated_at",
                    (pid, created),
                )
            return {"policy_version_id": pid, "version_number": version_number, "policy": p, "content_hash": computed_policy_hash, "policy_ref": policy_ref, "effective_from": effective_from, "effective_until": effective_until, "actor_ref": actor_ref, "prior_version_ref": prior, "change_reason": change_reason}

    install_policy = create_policy
    set_policy = create_policy

    def active_policy(self) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT v.* FROM governance_policy_pointer p JOIN governance_policy_versions v ON v.policy_version_id=p.policy_version_id WHERE p.pointer_id=1"
        ).fetchone()
        if not row:
            raise NotFound("no active governance policy")
        result = dict(row)
        result["policy"] = json.loads(result.pop("policy_json"))
        return result

    get_active_policy = active_policy

    def active_policy_version(self) -> GovernancePolicyVersion:
        """Return the active policy as its frozen contract, not an SQL projection."""
        row = self.connection.execute(
            "SELECT v.version_json FROM governance_policy_pointer p "
            "JOIN governance_policy_versions v ON v.policy_version_id=p.policy_version_id "
            "WHERE p.pointer_id=1"
        ).fetchone()
        if not row:
            raise NotFound("no active governance policy")
        return GovernancePolicyVersion.from_dict(json.loads(row[0]))

    def stage_change(
        self,
        change: Mapping[str, Any] | Any = None,
        *,
        change_id: str | None = None,
        thesis_id: str | None = None,
        content: Any = None,
        payload: Any = None,
        producer_invocation: Any = None,
        producer_invocation_id: str | None = None,
        actor_id: str | None = None,
    ) -> dict[str, Any]:
        if isinstance(change, str):
            data = {}
            change_id = change_id or change
        else:
            data = _mapping(change)
        if change_id is None:
            change_id = data.get("change_id", data.get("id"))
        thesis_id = thesis_id or data.get("thesis_id", data.get("thesis_ref"))
        if content is None:
            content = payload if payload is not None else data.get("content", data.get("payload", data.get("content_json")))
            if "content_json" in data and "content" not in data and "payload" not in data:
                try:
                    content = json.loads(content)
                except (TypeError, json.JSONDecodeError) as exc:
                    raise ValidationError("content_json must contain valid JSON") from exc
        if content is None:
            # A change mapping can itself be the content when explicit fields
            # were supplied by the caller.
            content = data.get("delta", data)
        producer = producer_invocation if producer_invocation is not None else data.get("producer_invocation")
        producer_id = producer_invocation_id or data.get("producer_invocation_id", data.get("producer_invocation_ref"))
        if producer is None and producer_id is None:
            producer = data.get("invocation")
        if not thesis_id:
            raise ValidationError("thesis_id is required")
        if producer is None and not producer_id:
            raise ValidationError("producer invocation is required")
        if actor_id is not None and (not isinstance(actor_id, str) or not actor_id):
            raise ValidationError("actor_id must be a non-empty string")
        cid = _id(change_id)
        if dataclasses.is_dataclass(content):
            content = dataclasses.asdict(content)
        if not isinstance(content, Mapping):
            raise ValidationError("thesis content must be a mapping")
        unknown = set(content) - _THESIS_FIELDS
        missing = _THESIS_FIELDS - set(content)
        if unknown:
            raise ValidationError(f"thesis content has unknown fields: {sorted(unknown)}")
        if missing:
            raise ValidationError(f"thesis content is missing fields: {sorted(missing)}")
        if content.get("confidence") not in _THESIS_CONFIDENCE_LEVELS:
            raise ValidationError("thesis confidence must be low, medium, or high")
        encoded = canonical_json(content)
        now = _now()
        with self._transaction() as cur:
            existing = cur.execute("SELECT * FROM staging_changes WHERE change_id=?", (cid,)).fetchone()
            if existing:
                raise ValidationError(f"staging change already exists: {cid}")
            if producer is None:
                producer = {"invocation_id": producer_id}
            inv = self._ensure_invocation(cur, producer, required_id=producer_id)
            cur.execute(
                "INSERT INTO staging_changes(change_id,thesis_id,content_json,content_hash,producer_invocation_id,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)",
                (cid, thesis_id, encoded, hashlib.sha256(encoded.encode()).hexdigest(), inv["invocation_id"], "staged", now, now),
            )
            event = {"change_id": cid, "thesis_id": thesis_id, "content_hash": hashlib.sha256(encoded.encode()).hexdigest(), "actor_id": actor_id}
            self._insert_event(cur, "staged", "thesis", thesis_id, event, change_id=cid, content_hash=event["content_hash"], actor_id=actor_id)
            return {"change_id": cid, "thesis_id": thesis_id, "content": json.loads(encoded), "content_hash": event["content_hash"], "producer_invocation_id": inv["invocation_id"], "status": "staged"}

    stage = stage_change

    def verify_change(
        self,
        change_id: str | Mapping[str, Any],
        verification: Mapping[str, Any] | Any = None,
        *,
        verification_id: str | None = None,
        verifier_invocation: Any = None,
        verifier_invocation_id: str | None = None,
        verdict: str | None = None,
        findings: Any = None,
        actor_id: str | None = None,
    ) -> dict[str, Any]:
        if isinstance(change_id, Mapping):
            data = dict(change_id)
            change_id = data.get("change_id", data.get("staging_id", data.get("target_ref", data.get("id"))))
            verification = verification or data
        else:
            data = _mapping(verification)
        verification = _mapping(verification)
        verifier = verifier_invocation if verifier_invocation is not None else data.get("verifier_invocation")
        verifier_id = verifier_invocation_id or data.get("verifier_invocation_id", data.get("verifier_invocation_ref"))
        if verifier is None:
            verifier = data.get("invocation")
        verdict = verdict if verdict is not None else data.get("verdict")
        verdict = _scalar(verdict)
        if verdict not in {v.value for v in Verdict}:
            raise ValidationError(f"invalid verdict: {verdict!r}")
        findings = findings if findings is not None else data.get("findings", data.get("result", {}))
        if not change_id or verifier is None and not verifier_id:
            raise ValidationError("change_id and verifier invocation are required")
        if not verdict:
            raise ValidationError("verdict is required")
        if actor_id is not None and (not isinstance(actor_id, str) or not actor_id):
            raise ValidationError("actor_id must be a non-empty string")
        vid = _id(verification_id or data.get("verification_id", data.get("id")))
        with self._transaction() as cur:
            stage = cur.execute("SELECT * FROM staging_changes WHERE change_id=?", (str(change_id),)).fetchone()
            if not stage:
                raise NotFound(f"staging change not found: {change_id}")
            if stage["status"] == "committed":
                raise ValidationError("cannot verify a committed change")
            if verifier is None:
                verifier = {"invocation_id": verifier_id}
            inv = self._ensure_invocation(cur, verifier, required_id=verifier_id)
            now = _now()
            policy = cur.execute("SELECT v.policy_version_id FROM governance_policy_pointer p JOIN governance_policy_versions v ON v.policy_version_id=p.policy_version_id WHERE p.pointer_id=1").fetchone()
            if not policy:
                raise GateRejected("no active governance policy")
            verification_doc = {
                "schema_version": "0.1", "id": vid, "created_at": now,
                "target_ref": str(change_id), "verifier_invocation_ref": inv["invocation_id"],
                "verdict": verdict, "findings": findings if isinstance(findings, list) else [findings],
                "deterministic_checks": data.get("deterministic_checks", []),
                "verifier_kind": data.get("verifier_kind", "model"),
                "revise_round": int(data.get("revise_round", 0)),
                "independence_policy_ref": policy[0],
                "subject_invocation_refs": [stage["producer_invocation_id"]],
                "target_content_hash": stage["content_hash"],
            }
            try:
                validated_verification = VerificationRecord.from_dict(verification_doc)
            except Exception as exc:
                raise ValidationError(str(exc)) from exc
            verification_doc = validated_verification.to_dict()
            encoded = canonical_json(verification_doc)
            cur.execute(
                "INSERT INTO verification_records(verification_id,change_id,producer_invocation_id,verifier_invocation_id,verdict,findings_json,verification_json,content_hash,policy_version_id,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                # The verification record attests to the staged content.  Its
                # content hash is therefore the staged hash, not a hash of the
                # verifier's findings envelope.
                (vid, str(change_id), stage["producer_invocation_id"], inv["invocation_id"], str(verdict), canonical_json(verification_doc["findings"]), encoded, stage["content_hash"], policy[0], now),
            )
            cur.execute("UPDATE staging_changes SET status='verified', updated_at=? WHERE change_id=?", (now, str(change_id)))
            self._insert_event(cur, "verified", "thesis", stage["thesis_id"], verification_doc, change_id=str(change_id), verification_id=vid, content_hash=stage["content_hash"], actor_id=actor_id)
            return {"verification_id": vid, "change_id": str(change_id), "verdict": str(verdict), "verifier_invocation_id": inv["invocation_id"], "status": "verified"}

    verify = verify_change

    def _insert_event(
        self,
        cur: sqlite3.Cursor,
        event_type: str,
        aggregate_type: str,
        aggregate_id: str,
        payload: Any,
        *,
        change_id: str | None = None,
        verification_id: str | None = None,
        version_id: str | None = None,
        content_hash: str | None = None,
        actor_id: str | None = None,
        idempotency_key: str | None = None,
        correlation_id: str | None = None,
    ) -> str:
        event_id = uuid.uuid4().hex
        occurred_at = _now()
        actor_id = actor_id or "system:dalton-core"
        specific_ref = version_id or verification_id or change_id or aggregate_id
        idempotency_key = idempotency_key or f"{event_type}:{specific_ref}"
        correlation_id = correlation_id or (change_id or aggregate_id)
        # A version_ref is reserved for an authoritative committed version.
        # Staged/verified events carry their own explicit refs in the envelope.
        version_ref = version_id
        if content_hash is None:
            content_hash = globals()["content_hash"](payload)
        aggregate_version = int(cur.execute(
            "SELECT COALESCE(MAX(aggregate_version),0)+1 FROM domain_events WHERE aggregate_type=? AND aggregate_id=?",
            (aggregate_type, aggregate_id),
        ).fetchone()[0])
        envelope = {
            "schema_version": "0.1",
            "id": event_id,
            "created_at": occurred_at,
            "event_type": event_type,
            "aggregate_type": aggregate_type,
            "aggregate_id": aggregate_id,
            "aggregate_version": aggregate_version,
            "version_ref": version_ref,
            "change_ref": change_id,
            "verification_ref": verification_id,
            "content_hash": content_hash,
            "occurred_at": occurred_at,
            "actor_ref": actor_id,
            "payload": payload,
            "idempotency_key": idempotency_key,
            "correlation_id": correlation_id,
        }
        cur.execute(
            "INSERT INTO domain_events(event_id,event_type,aggregate_type,aggregate_id,change_id,verification_id,version_id,aggregate_version,version_ref,content_hash,actor_id,idempotency_key,correlation_id,occurred_at,event_json,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (event_id, event_type, aggregate_type, aggregate_id, change_id, verification_id, version_id, aggregate_version, version_ref, content_hash, actor_id, idempotency_key, correlation_id, occurred_at, canonical_json(envelope), occurred_at),
        )
        return event_id

    def _commit_request_hash(self, change_id: str, verification_id: str, request: Any, stage_hash: str) -> str:
        return content_hash({"change_id": change_id, "verification_id": verification_id, "content_hash": stage_hash, "request": request})

    def commit(
        self,
        change_id: str | None = None,
        verification_id: str | None = None,
        idempotency_key: str | None = None,
        *,
        request: Any = None,
        request_hash: str | None = None,
        actor_id: str | None = None,
        fault_at: str | None = None,
    ) -> dict[str, Any]:
        if not change_id or not verification_id or not idempotency_key:
            raise ValidationError("change_id, verification_id and idempotency_key are required")
        if actor_id is not None and (not isinstance(actor_id, str) or not actor_id):
            raise ValidationError("actor_id must be a non-empty string")
        with self._transaction() as cur:
            stage = cur.execute("SELECT * FROM staging_changes WHERE change_id=?", (change_id,)).fetchone()
            if not stage:
                raise NotFound(f"staging change not found: {change_id}")
            if cur.execute(
                "SELECT 1 FROM thesis_admission_candidates WHERE thesis_ref=? LIMIT 1",
                (stage["thesis_id"],),
            ).fetchone():
                raise GateRejected(
                    "coverage-governed thesis creation and revision require human admission authority"
                )
            actual_stage_hash = content_hash(json.loads(stage["content_json"]))
            if actual_stage_hash != stage["content_hash"]:
                raise GateRejected("staged content no longer matches its content hash")
            computed_hash = self._commit_request_hash(change_id, verification_id, request, stage["content_hash"])
            if request_hash is not None and request_hash != computed_hash:
                raise ValidationError("caller request_hash does not match the canonical commit request")
            prior_key = cur.execute("SELECT * FROM idempotency_keys WHERE idempotency_key=?", (idempotency_key,)).fetchone()
            if prior_key:
                if prior_key["request_hash"] == computed_hash:
                    original = json.loads(prior_key["result_json"])
                    return {**original, "status": "duplicate", "idempotency_key": idempotency_key}
                return {"status": "conflict", "idempotency_key": idempotency_key, "existing_request_hash": prior_key["request_hash"], "request_hash": computed_hash}
            if stage["status"] != "verified":
                raise VerificationRequired("staging change has no committed verification")
            verification = cur.execute("SELECT * FROM verification_records WHERE verification_id=? AND change_id=?", (verification_id, change_id)).fetchone()
            if not verification:
                raise VerificationRequired("verification record not found for staging change")
            if verification["content_hash"] != stage["content_hash"]:
                raise GateRejected("verification content hash does not match staged content hash")
            if verification["producer_invocation_id"] != stage["producer_invocation_id"]:
                raise GateRejected("staged producer no longer matches verification provenance")
            event_rows = cur.execute(
                "SELECT event_type,aggregate_id,content_hash,verification_id FROM domain_events "
                "WHERE change_id=? AND event_type IN ('staged','verified') ORDER BY aggregate_version",
                (change_id,),
            ).fetchall()
            staged_events = [row for row in event_rows if row["event_type"] == "staged"]
            verified_events = [row for row in event_rows if row["event_type"] == "verified" and row["verification_id"] == verification_id]
            if len(staged_events) != 1 or len(verified_events) != 1:
                raise GateRejected("staging and verification event provenance is incomplete")
            if any(row["aggregate_id"] != stage["thesis_id"] or row["content_hash"] != stage["content_hash"] for row in (*staged_events, *verified_events)):
                raise GateRejected("staging identity no longer matches event provenance")
            producer = cur.execute("SELECT * FROM model_invocations WHERE invocation_id=?", (stage["producer_invocation_id"],)).fetchone()
            verifier = cur.execute("SELECT * FROM model_invocations WHERE invocation_id=?", (verification["verifier_invocation_id"],)).fetchone()
            if not producer or not verifier:
                raise VerificationRequired("producer/verifier invocation provenance is missing")
            policy_row = cur.execute("SELECT v.policy_version_id, v.policy_json, v.content_hash, v.effective_from, v.effective_until FROM governance_policy_pointer p JOIN governance_policy_versions v ON v.policy_version_id=p.policy_version_id WHERE p.pointer_id=1").fetchone()
            if not policy_row:
                raise GateRejected("no active governance policy")
            if verification["policy_version_id"] != policy_row[0]:
                raise GateRejected("verification was produced under a policy that is no longer active")
            self._assert_policy_effective(policy_row)
            ok, reason = evaluate_gate(json.loads(policy_row[1]), verification["verdict"], dict(producer), dict(verifier))
            if not ok:
                if "verdict" in (reason or ""):
                    raise BadVerdict(reason)
                if "verification" in (reason or ""):
                    raise VerificationRequired(reason)
                raise IndependenceViolation(reason)
            current = cur.execute("SELECT * FROM current_pointers WHERE thesis_id=?", (stage["thesis_id"],)).fetchone()
            if current:
                prior_id = current["version_id"]
                version_number = int(current["version_number"]) + 1
            else:
                prior_id = None
                version_number = 1
            version_id = uuid.uuid4().hex
            now = _now()
            actor_ref = actor_id or "system:dalton-core"
            thesis_payload = json.loads(stage["content_json"])
            claim_refs = thesis_payload.get("claim_refs", [])
            for claim_ref in claim_refs:
                if not isinstance(claim_ref, str) or not claim_ref:
                    raise ValidationError("thesis claim_refs must contain non-empty ClaimVersion ids")
                claim_row = cur.execute("SELECT claim_version_id FROM claim_versions WHERE claim_version_id=?", (claim_ref,)).fetchone()
                if not claim_row:
                    raise GateRejected(f"thesis claim_ref does not resolve to a ClaimVersion: {claim_ref}")
                if not cur.execute("SELECT 1 FROM evidence_relations WHERE claim_version_id=? LIMIT 1", (claim_ref,)).fetchone():
                    raise GateRejected(f"thesis ClaimVersion has no EvidenceRelation: {claim_ref}")
            thesis_wire = {
                "schema_version": "0.2", "id": version_id, "created_at": now,
                "thesis_ref": stage["thesis_id"], "version": version_number,
                **thesis_payload, "prior_version_ref": prior_id,
                "authority_kind": "verification", "authority_ref": verification_id,
                "committed_by_ref": actor_ref,
                "content_hash": stage["content_hash"],
            }
            try:
                validated_thesis = ThesisVersion.from_dict(thesis_wire)
            except Exception as exc:
                raise ValidationError(str(exc)) from exc
            thesis_encoded = canonical_json(validated_thesis.to_dict())
            cur.execute(
                "INSERT INTO thesis_versions(version_id,thesis_id,version_number,content_json,content_hash,prior_version_id,change_id,verification_id,admission_decision_id,authority_kind,authority_ref,committed_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (version_id, stage["thesis_id"], version_number, thesis_encoded, stage["content_hash"], prior_id, change_id, verification_id, None, "verification", verification_id, actor_ref, now),
            )
            if fault_at == "after_version":
                raise RuntimeError("injected commit failure after version insert")
            event_id = self._insert_event(cur, "committed", "thesis", stage["thesis_id"], {"version_id": version_id, "content_hash": stage["content_hash"], "policy_version_id": policy_row[0], "policy_content_hash": policy_row[2]}, change_id=change_id, verification_id=verification_id, version_id=version_id, content_hash=stage["content_hash"], actor_id=actor_id, idempotency_key=idempotency_key)
            if fault_at == "after_event":
                raise RuntimeError("injected commit failure after event insert")
            pointer_values = (stage["thesis_id"], version_id, version_number, stage["content_hash"], now)
            if current:
                cur.execute("UPDATE current_pointers SET version_id=?,version_number=?,content_hash=?,updated_at=? WHERE thesis_id=?", (version_id, version_number, stage["content_hash"], now, stage["thesis_id"]))
            else:
                cur.execute("INSERT INTO current_pointers(thesis_id,version_id,version_number,content_hash,updated_at) VALUES(?,?,?,?,?)", pointer_values)
            if fault_at == "after_pointer":
                raise RuntimeError("injected commit failure after pointer update")
            result = {"status": "fresh", "idempotency_key": idempotency_key, "version_id": version_id, "thesis_id": stage["thesis_id"], "version_number": version_number, "prior_version_id": prior_id, "event_id": event_id, "content_hash": stage["content_hash"]}
            cur.execute("INSERT INTO idempotency_keys(idempotency_key,request_hash,result_json,version_id,created_at) VALUES(?,?,?,?,?)", (idempotency_key, computed_hash, canonical_json(result), version_id, now))
            cur.execute("UPDATE staging_changes SET status='committed',updated_at=? WHERE change_id=?", (now, change_id))
            return result

    commit_change = commit

    def current_pointer(self, thesis_id: str) -> dict[str, Any] | None:
        return self._row(self.connection.execute("SELECT * FROM current_pointers WHERE thesis_id=?", (thesis_id,)).fetchone())

    get_current_pointer = current_pointer

    def get_version(self, version_id: str) -> dict[str, Any] | None:
        row = self.connection.execute("SELECT * FROM thesis_versions WHERE version_id=?", (version_id,)).fetchone()
        result = self._row(row)
        if result:
            result["content"] = json.loads(result["content_json"])
        return result

    # ------------------------------------------------------------------
    # Research ledger: Evidence -> Claim -> Adjudication
    # ------------------------------------------------------------------
    @staticmethod
    def _ledger_hash(wire: Mapping[str, Any]) -> str:
        base = dict(wire)
        base.pop("content_hash", None)
        return content_hash(base)

    @staticmethod
    def _ledger_payload(value: Any) -> dict[str, Any]:
        data = _mapping(value)
        if hasattr(value, "to_dict"):
            data = dict(value.to_dict())
        return data

    @staticmethod
    def _row_json(row: sqlite3.Row, key: str) -> dict[str, Any]:
        return json.loads(row[key])

    def commit_reviewed_candidate(
        self,
        *,
        decision: Mapping[str, Any],
        evidence: Mapping[str, Any],
        claim: Mapping[str, Any],
        idempotency_key: str,
        fault_at: str | None = None,
    ) -> dict[str, Any]:
        """Atomically promote one explicitly accepted candidate into Ledger 0.2.

        The scoped writer boundary authenticates the review-control service;
        this method still revalidates every closed record and derives the
        producer execution from Core's SourceEnvelope authority.  It never
        accepts a caller-supplied execution id, projected claim status, or
        auto-accept authorization.
        """
        from .research_review import validate_human_review_decision

        decision_wire = validate_human_review_decision(decision)
        if decision_wire["verdict"] != "accept":
            raise GateRejected("only an explicit accepted review can enter the Ledger")
        return self._commit_authorized_candidate(
            decision_wire=decision_wire,
            evidence=evidence,
            claim=claim,
            idempotency_key=idempotency_key,
            fault_at=fault_at,
            active_policy_binding=None,
        )

    def commit_policy_candidate(
        self,
        *,
        evidence: Mapping[str, Any],
        claim: Mapping[str, Any],
        material: Mapping[str, Any] | None = None,
        numeric_spec: Mapping[str, Any] | None = None,
        source_verification: Mapping[str, Any] | None = None,
        numeric_verification: Mapping[str, Any] | None = None,
        idempotency_key: str,
        fault_at: str | None = None,
    ) -> dict[str, Any]:
        """Promote one low-risk candidate authorized by the active policy.

        The evaluator re-derives the complete SEC filing-count statement from
        Core's immutable connector authority.  A caller cannot select another
        source, metric, statement, record count, policy version, or actor.
        """
        from .research_auto_commit import authorize_policy_candidate

        policy = self.active_policy()
        decision_wire = authorize_policy_candidate(
            connection=self.connection,
            policy_version=policy,
            evidence=evidence,
            claim=claim,
            material=material,
            numeric_spec=numeric_spec,
            source_verification=source_verification,
            numeric_verification=numeric_verification,
        )
        result = self._commit_authorized_candidate(
            decision_wire=decision_wire,
            evidence=evidence,
            claim=claim,
            idempotency_key=idempotency_key,
            fault_at=fault_at,
            active_policy_binding=(
                decision_wire["policy_version_ref"],
                decision_wire["policy_version_hash"],
            ),
        )
        return {**result, "authorization": decision_wire}

    def _commit_authorized_candidate(
        self,
        *,
        decision_wire: Mapping[str, Any],
        evidence: Mapping[str, Any],
        claim: Mapping[str, Any],
        idempotency_key: str,
        fault_at: str | None,
        active_policy_binding: tuple[str, str] | None,
    ) -> dict[str, Any]:
        """Shared atomic Ledger writer for human and policy authorization."""
        from .research_review import (
            validate_claim_version_v0_2,
            validate_evidence_version_v0_2,
        )
        from .research_verification import (
            validate_candidate_claim,
            validate_candidate_evidence,
        )

        decision_wire = dict(decision_wire)
        evidence_wire = validate_candidate_evidence(evidence)
        claim_wire = validate_candidate_claim(claim)
        if decision_wire["verdict"] != "accept":
            raise GateRejected("only an accepted authorization can enter the Ledger")
        if not isinstance(idempotency_key, str) or not idempotency_key:
            raise ValidationError("idempotency_key is required")
        if decision_wire["reviewer_ref"] == claim_wire["actor_ref"]:
            raise IndependenceViolation("candidate producer cannot review its own claim")
        expected_evidence = [{"ref": evidence_wire["id"], "hash": evidence_wire["content_hash"]}]
        if claim_wire["candidate_evidence_refs"] != expected_evidence:
            raise GateRejected("candidate claim does not bind the reviewed evidence")
        if (
            decision_wire["candidate_claim_ref"] != claim_wire["id"]
            or decision_wire["candidate_claim_hash"] != claim_wire["content_hash"]
            or decision_wire["candidate_evidence_ref"] != evidence_wire["id"]
            or decision_wire["candidate_evidence_hash"] != evidence_wire["content_hash"]
        ):
            raise GateRejected("review decision does not bind the exact candidate pair")
        reviewed_semantics = {
            field: claim_wire[field]
            for field in (
                "subject_ref", "metric_or_aspect", "period", "basis", "normalized_statement"
            )
        }
        if canonical_json(decision_wire["reviewed_semantics"]) != canonical_json(reviewed_semantics):
            raise GateRejected("review decision semantics drifted from the candidate")
        if (
            evidence_wire["source_verification_ref"] != claim_wire["source_verification_ref"]
            or evidence_wire["source_verification_hash"] != claim_wire["source_verification_hash"]
        ):
            raise GateRejected("candidate source verification binding is inconsistent")
        if evidence_wire["source_type"] == "recorded_fixture":
            raise GateRejected("recorded fixture candidates cannot enter the formal Ledger")

        request_hash = content_hash({
            "decision_hash": decision_wire["content_hash"],
            "evidence_hash": evidence_wire["content_hash"],
            "claim_hash": claim_wire["content_hash"],
        })
        with self._transaction() as cur:
            prior_key = cur.execute(
                "SELECT request_hash,result_json FROM reviewed_candidate_commits "
                "WHERE idempotency_key=?", (idempotency_key,),
            ).fetchone()
            if prior_key is not None:
                if prior_key["request_hash"] != request_hash:
                    return {
                        "status": "conflict", "idempotency_key": idempotency_key,
                        "existing_request_hash": prior_key["request_hash"],
                        "request_hash": request_hash,
                    }
                original = json.loads(prior_key["result_json"])
                return {**original, "status": "duplicate", "idempotency_key": idempotency_key}

            prior_decision = cur.execute(
                "SELECT request_hash,result_json FROM reviewed_candidate_commits "
                "WHERE review_decision_ref=? OR candidate_evidence_ref=? OR candidate_claim_ref=?",
                (decision_wire["id"], evidence_wire["id"], claim_wire["id"]),
            ).fetchone()
            if prior_decision is not None:
                if prior_decision["request_hash"] == request_hash:
                    original = json.loads(prior_decision["result_json"])
                    return {**original, "status": "duplicate", "idempotency_key": idempotency_key}
                raise IdempotencyConflict("review decision or candidate was already promoted")

            if active_policy_binding is not None:
                policy_row = cur.execute(
                    "SELECT v.policy_version_id,v.content_hash,v.effective_from,v.effective_until "
                    "FROM governance_policy_pointer p JOIN governance_policy_versions v "
                    "ON v.policy_version_id=p.policy_version_id WHERE p.pointer_id=1"
                ).fetchone()
                if policy_row is None:
                    raise GateRejected("no active governance policy")
                if (policy_row["policy_version_id"], policy_row["content_hash"]) != active_policy_binding:
                    raise GateRejected("research authorization policy is no longer active")
                self._assert_policy_effective(policy_row)

            required_authority_tables = (
                "connector_invocations", "connector_source_envelopes",
                "observability_artifact_version_index",
                "observability_artifact_versions_v2",
            )
            present_tables = {
                row["name"] for row in cur.execute(
                    "SELECT name FROM sqlite_master WHERE type='table' AND name IN (?,?,?,?)",
                    required_authority_tables,
                ).fetchall()
            }
            if present_tables != set(required_authority_tables):
                # A Core that never opened its connector authority cannot verify
                # any candidate SourceEnvelope.  Reject explicitly instead of
                # surfacing a raw sqlite error from a missing table.
                raise GateRejected(
                    "Core connector authority is unavailable; "
                    "candidate SourceEnvelope cannot be verified"
                )
            source = cur.execute(
                "SELECT connector_invocation_ref,record_json,content_hash FROM "
                "connector_source_envelopes WHERE source_envelope_id=?",
                (evidence_wire["source_envelope_ref"],),
            ).fetchone()
            if source is None or source["content_hash"] != evidence_wire["source_envelope_hash"]:
                raise GateRejected("candidate SourceEnvelope is not exact Core authority")
            source_doc = json.loads(source["record_json"])
            if (
                source_doc.get("source") != evidence_wire["source_ref"]
                or source_doc.get("raw_artifact_version_ref")
                != evidence_wire["artifact_refs"][0]["ref"]
            ):
                raise GateRejected("candidate source/artifact binding drifted from Core authority")
            invocation = cur.execute(
                "SELECT execution_ref FROM connector_invocations WHERE connector_invocation_id=?",
                (source["connector_invocation_ref"],),
            ).fetchone()
            if invocation is None:
                raise GateRejected("candidate producer execution is unavailable")
            producer_execution_ref = invocation["execution_ref"]
            artifact = cur.execute(
                "SELECT i.version_id,v.content_hash,v.artifact_content_hash,v.record_json "
                "FROM observability_artifact_version_index i "
                "JOIN observability_artifact_versions_v2 v ON v.version_id=i.version_id "
                "WHERE i.version_id=? AND i.producer_execution_ref=?",
                (evidence_wire["artifact_refs"][0]["ref"], producer_execution_ref),
            ).fetchone()
            if (
                artifact is None
                or artifact["content_hash"] != evidence_wire["artifact_refs"][0]["hash"]
            ):
                raise GateRejected("candidate ArtifactVersion is not exact Core authority")
            from .transcript_correction import (
                TRANSCRIPT_EVIDENCE_SOURCE_TYPE,
                TranscriptCorrectionError,
                validate_persisted_transcript_claim_citation,
            )

            if evidence_wire["source_type"] == TRANSCRIPT_EVIDENCE_SOURCE_TYPE:
                if (
                    len(evidence_wire["artifact_refs"]) != 2
                    or not evidence_wire["artifact_refs"][1]["ref"].startswith(
                        "transcript-claim-citation-binding:"
                    )
                    or evidence_wire["source_lineage"][-1]
                    != evidence_wire["artifact_refs"][1]["ref"]
                ):
                    raise GateRejected(
                        "transcript candidate is missing its exact citation binding"
                    )
                citation_ref = evidence_wire["artifact_refs"][1]
                try:
                    citation = validate_persisted_transcript_claim_citation(
                        self.connection,
                        citation_ref["ref"],
                        citation_ref["hash"],
                    )
                except (TranscriptCorrectionError, sqlite3.Error) as exc:
                    raise GateRejected(
                        "transcript candidate citation authority is unavailable or invalid"
                    ) from exc
                correction_row = cur.execute(
                    "SELECT record_json FROM transcript_correction_set_versions "
                    "WHERE version_id=?",
                    (citation["correction_set_version_ref"],),
                ).fetchone()
                correction_set = (
                    None if correction_row is None
                    else json.loads(correction_row["record_json"])
                )
                document_ref = (
                    None if not isinstance(correction_set, Mapping)
                    else correction_set.get("document_ref")
                )
                alphaengine_document_binding = (
                    source_doc.get("source") == "source:alphaengine"
                    and source_doc.get("operation") == "get_document"
                    and isinstance(document_ref, str)
                    and document_ref.startswith("alphaengine-doc:")
                    and source_doc.get("source_record_refs") == [
                        f"{document_ref}:sha256:{citation['source_content_hash']}"
                    ]
                    and source_doc.get("raw_response_hash")
                    == artifact["artifact_content_hash"]
                )
                direct_raw_binding = (
                    citation["source_content_hash"]
                    == artifact["artifact_content_hash"]
                    and source_doc.get("raw_response_hash")
                    == citation["source_content_hash"]
                )
                if not (alphaengine_document_binding or direct_raw_binding):
                    raise GateRejected(
                        "transcript citation does not bind the exact raw ArtifactVersion"
                    )

            evidence_ref = evidence_wire["candidate_evidence_ref"].replace(
                "candidate-evidence:", "evidence:", 1
            )
            claim_ref = claim_wire["candidate_claim_ref"].replace(
                "candidate-claim:", "claim:", 1
            )
            previous_evidence = cur.execute(
                "SELECT evidence_version_id,version_number FROM evidence_versions "
                "WHERE evidence_ref=? ORDER BY version_number DESC LIMIT 1", (evidence_ref,),
            ).fetchone()
            previous_claim = cur.execute(
                "SELECT claim_version_id,version_number FROM claim_versions "
                "WHERE claim_ref=? ORDER BY version_number DESC LIMIT 1", (claim_ref,),
            ).fetchone()
            # Candidate revisions are staging-local semantic work.  Their
            # version numbers do not reserve or skip versions in the formal
            # Ledger; the first accepted candidate is formal version 1 even
            # when a rejected/revised staging predecessor exists.
            expected_evidence_version = 1 if previous_evidence is None else int(previous_evidence["version_number"]) + 1
            expected_claim_version = 1 if previous_claim is None else int(previous_claim["version_number"]) + 1
            evidence_version_id = "evidence-version:" + content_hash({
                "candidate": evidence_wire["id"], "review": decision_wire["id"]
            })
            claim_version_id = "claim-version:" + content_hash({
                "candidate": claim_wire["id"], "review": decision_wire["id"]
            })
            evidence_v2 = {
                "schema_version": "0.2", "id": evidence_version_id,
                "created_at": decision_wire["created_at"], "evidence_ref": evidence_ref,
                "version": expected_evidence_version, "source_type": evidence_wire["source_type"],
                "source_ref": evidence_wire["source_ref"],
                "source_envelope_ref": evidence_wire["source_envelope_ref"],
                "source_envelope_hash": evidence_wire["source_envelope_hash"],
                "retrieved_at": evidence_wire["retrieved_at"],
                "valid_until": evidence_wire["valid_until"],
                "artifact_refs": list(evidence_wire["artifact_refs"]),
                "source_lineage": list(evidence_wire["source_lineage"]),
                "independence_group": evidence_wire["independence_group"],
                "source_verification_ref": evidence_wire["source_verification_ref"],
                "source_verification_hash": evidence_wire["source_verification_hash"],
                "candidate_origin_ref": evidence_wire["id"],
                "candidate_origin_hash": evidence_wire["content_hash"],
                "review_decision_ref": decision_wire["id"],
                "review_decision_hash": decision_wire["content_hash"],
                "actor_ref": decision_wire["reviewer_ref"],
                "prior_version_ref": None if previous_evidence is None else previous_evidence["evidence_version_id"],
            }
            evidence_v2["content_hash"] = self._ledger_hash(evidence_v2)
            evidence_v2 = validate_evidence_version_v0_2(evidence_v2)
            claim_v2 = {
                "schema_version": "0.2", "id": claim_version_id,
                "created_at": decision_wire["created_at"], "claim_ref": claim_ref,
                "version": expected_claim_version, "subject_ref": claim_wire["subject_ref"],
                "metric_or_aspect": claim_wire["metric_or_aspect"],
                "period": claim_wire["period"], "basis": claim_wire["basis"],
                "normalized_statement": claim_wire["normalized_statement"],
                "claim_kind": claim_wire["claim_kind"], "value": claim_wire["value"],
                "unit": claim_wire["unit"], "currency": claim_wire["currency"],
                "scale": claim_wire["scale"],
                "producer_execution_refs": [producer_execution_ref],
                "semantic_review_ref": decision_wire["id"],
                "semantic_review_hash": decision_wire["content_hash"],
                "candidate_origin_ref": claim_wire["id"],
                "candidate_origin_hash": claim_wire["content_hash"],
                "actor_ref": decision_wire["reviewer_ref"],
                "prior_version_ref": None if previous_claim is None else previous_claim["claim_version_id"],
            }
            claim_v2["content_hash"] = self._ledger_hash(claim_v2)
            claim_v2 = validate_claim_version_v0_2(claim_v2)

            cur.execute(
                "INSERT INTO evidence_versions(evidence_version_id,evidence_ref,version_number,"
                "evidence_json,content_hash,prior_version_id,created_at) VALUES(?,?,?,?,?,?,?)",
                (
                    evidence_v2["id"], evidence_v2["evidence_ref"], evidence_v2["version"],
                    canonical_json(evidence_v2), evidence_v2["content_hash"],
                    evidence_v2["prior_version_ref"], evidence_v2["created_at"],
                ),
            )
            evidence_event = self._insert_event(
                cur, "evidence_versioned", "evidence", evidence_v2["evidence_ref"],
                evidence_v2, content_hash=evidence_v2["content_hash"],
                actor_id=decision_wire["reviewer_ref"],
                idempotency_key=f"reviewed-evidence:{decision_wire['id']}",
            )
            if fault_at == "after_evidence":
                raise RuntimeError("injected reviewed candidate failure after evidence")
            cur.execute(
                "INSERT INTO claim_versions(claim_version_id,claim_ref,version_number,claim_json,"
                "content_hash,prior_version_id,created_at) VALUES(?,?,?,?,?,?,?)",
                (
                    claim_v2["id"], claim_v2["claim_ref"], claim_v2["version"],
                    canonical_json(claim_v2), claim_v2["content_hash"],
                    claim_v2["prior_version_ref"], claim_v2["created_at"],
                ),
            )
            claim_event = self._insert_event(
                cur, "claim_versioned", "claim", claim_v2["claim_ref"], claim_v2,
                content_hash=claim_v2["content_hash"], actor_id=decision_wire["reviewer_ref"],
                idempotency_key=f"reviewed-claim:{decision_wire['id']}",
            )
            self._emit_numeric_challenges_document(cur, claim_v2)
            if fault_at == "after_claim":
                raise RuntimeError("injected reviewed candidate failure after claim")

            relation_wire = {
                "schema_version": "0.1",
                "id": "relation:reviewed:" + content_hash({
                    "evidence": evidence_v2["id"], "claim": claim_v2["id"],
                    "review": decision_wire["id"],
                }),
                "created_at": decision_wire["created_at"],
                "evidence_ref": evidence_v2["evidence_ref"],
                "evidence_version_ref": evidence_v2["id"],
                "claim_ref": claim_v2["claim_ref"],
                "claim_version_ref": claim_v2["id"],
                "relation": "supports",
                "source_lineage": list(evidence_v2["source_lineage"]),
                "independence_group": evidence_v2["independence_group"],
                "actor_ref": decision_wire["reviewer_ref"],
            }
            relation_wire["content_hash"] = self._ledger_hash(relation_wire)
            relation = EvidenceRelation.from_dict(relation_wire)
            relation_doc = relation.to_dict()
            cur.execute(
                "INSERT INTO evidence_relations(relation_id,evidence_ref,evidence_version_id,"
                "claim_ref,claim_version_id,relation,relation_json,content_hash,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    relation.id, relation.evidence_ref, relation.evidence_version_ref,
                    relation.claim_ref, relation.claim_version_ref, relation.relation.value,
                    canonical_json(relation_doc), relation.content_hash, relation.created_at,
                ),
            )
            relation_event = self._insert_event(
                cur, "evidence_related", "claim", claim_v2["claim_ref"], relation_doc,
                content_hash=relation.content_hash, actor_id=decision_wire["reviewer_ref"],
                idempotency_key=f"reviewed-relation:{decision_wire['id']}",
            )
            if fault_at == "after_relation":
                raise RuntimeError("injected reviewed candidate failure after relation")
            result = {
                "status": "fresh", "idempotency_key": idempotency_key,
                "review_decision_ref": decision_wire["id"],
                "evidence_version_ref": evidence_v2["id"],
                "claim_version_ref": claim_v2["id"], "relation_ref": relation.id,
                "claim_status": self._claim_status(cur, claim_v2["id"]),
                "event_refs": [evidence_event, claim_event, relation_event],
            }
            cur.execute(
                "INSERT INTO reviewed_candidate_commits(idempotency_key,request_hash,"
                "review_decision_ref,candidate_evidence_ref,candidate_claim_ref,decision_json,"
                "result_json,created_at) VALUES(?,?,?,?,?,?,?,?)",
                (
                    idempotency_key, request_hash, decision_wire["id"], evidence_wire["id"],
                    claim_wire["id"], canonical_json(decision_wire), canonical_json(result),
                    decision_wire["created_at"],
                ),
            )
            if fault_at == "after_receipt":
                raise RuntimeError("injected reviewed candidate failure after receipt")
            return result

    def register_evidence(
        self,
        evidence: Mapping[str, Any] | Any = None,
        *,
        evidence_ref: str | None = None,
        evidence_id: str | None = None,
        evidence_version_id: str | None = None,
        actor_ref: str | None = None,
    ) -> dict[str, Any]:
        """Append an immutable evidence version and its domain event."""
        data = self._ledger_payload(evidence)
        stable_ref = evidence_ref or data.get("evidence_ref") or evidence_id or data.get("id")
        if not stable_ref:
            raise ValidationError("evidence_ref is required")
        version_id = evidence_version_id or data.get("version_id") or (data.get("id") if data.get("evidence_ref") else None)
        created = data.get("created_at") or _now()
        with self._transaction() as cur:
            previous = cur.execute(
                "SELECT * FROM evidence_versions WHERE evidence_ref=? ORDER BY version_number DESC LIMIT 1", (str(stable_ref),)
            ).fetchone()
            version = (int(previous["version_number"]) + 1) if previous else 1
            prior = previous["evidence_version_id"] if previous else None
            supplied_version = data.get("version")
            if supplied_version is not None:
                try:
                    supplied_version_int = int(supplied_version)
                except (TypeError, ValueError) as exc:
                    raise ValidationError("evidence version must be an integer") from exc
                if isinstance(supplied_version, bool) or supplied_version_int != version:
                    raise ValidationError(f"evidence version must be the next chain version ({version})")
            supplied_prior = data.get("prior_version_ref")
            if supplied_prior is not None and supplied_prior != prior:
                raise ValidationError("evidence prior_version_ref does not match current version")
            wire = {
                "schema_version": data.get("schema_version", "0.1"),
                "id": version_id or uuid.uuid4().hex,
                "created_at": created,
                "evidence_ref": str(stable_ref),
                "version": version,
                "source_type": data.get("source_type"),
                "source_ref": data.get("source_ref"),
                "retrieved_at": data.get("retrieved_at") or created,
                "valid_until": data.get("valid_until"),
                "artifact_refs": list(data.get("artifact_refs", [])),
                "source_lineage": list(data.get("source_lineage", [data.get("source_ref")] if data.get("source_ref") else [])),
                "independence_group": data.get("independence_group"),
                "actor_ref": actor_ref or data.get("actor_ref", "system:dalton-core"),
                "prior_version_ref": prior,
            }
            if wire["source_type"] is None or wire["source_ref"] is None or wire["independence_group"] is None:
                raise ValidationError("evidence source_type, source_ref and independence_group are required")
            wire["content_hash"] = self._ledger_hash(wire)
            supplied_hash = data.get("content_hash")
            if supplied_hash is not None and supplied_hash != wire["content_hash"]:
                raise ValidationError("evidence content_hash does not match canonical version")
            try:
                validated = EvidenceVersion.from_dict(wire)
            except Exception as exc:
                raise ValidationError(str(exc)) from exc
            encoded = canonical_json(validated.to_dict())
            try:
                cur.execute(
                    "INSERT INTO evidence_versions(evidence_version_id,evidence_ref,version_number,evidence_json,content_hash,prior_version_id,created_at) VALUES(?,?,?,?,?,?,?)",
                    (validated.id, validated.evidence_ref, validated.version, encoded, validated.content_hash, validated.prior_version_ref, validated.created_at),
                )
            except sqlite3.IntegrityError as exc:
                raise IdempotencyConflict("evidence version id or version number already exists") from exc
            event_id = self._insert_event(cur, "evidence_versioned", "evidence", validated.evidence_ref, validated.to_dict(), content_hash=validated.content_hash, actor_id=validated.actor_ref, idempotency_key=f"evidence:{validated.id}")
            return {"evidence_ref": validated.evidence_ref, "evidence_version_id": validated.id, "version": validated.version, "content_hash": validated.content_hash, "event_id": event_id}

    add_evidence = register_evidence
    create_evidence = register_evidence
    create_evidence_version = register_evidence

    def register_claim(
        self,
        claim: Mapping[str, Any] | Any = None,
        *,
        claim_ref: str | None = None,
        claim_id: str | None = None,
        claim_version_id: str | None = None,
        actor_ref: str | None = None,
    ) -> dict[str, Any]:
        """Append an immutable claim version; status is never caller supplied."""
        data = self._ledger_payload(claim)
        stable_ref = claim_ref or data.get("claim_ref") or claim_id or data.get("id")
        if not stable_ref:
            raise ValidationError("claim_ref is required")
        if "status" in data:
            raise ValidationError("claim status is a projection and cannot be written")
        version_id = claim_version_id or data.get("version_id") or (data.get("id") if data.get("claim_ref") else None)
        created = data.get("created_at") or _now()
        with self._transaction() as cur:
            previous = cur.execute("SELECT * FROM claim_versions WHERE claim_ref=? ORDER BY version_number DESC LIMIT 1", (str(stable_ref),)).fetchone()
            version = int(previous["version_number"]) + 1 if previous else 1
            prior = previous["claim_version_id"] if previous else None
            supplied_version = data.get("version")
            if supplied_version is not None:
                try:
                    supplied_version_int = int(supplied_version)
                except (TypeError, ValueError) as exc:
                    raise ValidationError("claim version must be an integer") from exc
                if isinstance(supplied_version, bool) or supplied_version_int != version:
                    raise ValidationError(f"claim version must be the next chain version ({version})")
            if data.get("prior_version_ref") is not None and data["prior_version_ref"] != prior:
                raise ValidationError("claim prior_version_ref does not match current version")
            wire = {
                "schema_version": data.get("schema_version", "0.1"), "id": version_id or uuid.uuid4().hex,
                "created_at": created, "claim_ref": str(stable_ref), "version": version,
                "subject_ref": data.get("subject_ref"), "metric_or_aspect": data.get("metric_or_aspect", data.get("metric")),
                "period": data.get("period"), "basis": data.get("basis"), "normalized_statement": data.get("normalized_statement", data.get("statement")),
                "claim_kind": _scalar(data.get("claim_kind", "qualitative")), "value": data.get("value"), "unit": data.get("unit"),
                "producer_invocation_refs": list(data.get("producer_invocation_refs", [])),
                "actor_ref": data.get("actor_ref", actor_ref or "system:dalton-core"), "prior_version_ref": prior,
            }
            required = ("subject_ref", "metric_or_aspect", "period", "basis", "normalized_statement")
            if any(not isinstance(wire[name], str) or not wire[name] for name in required):
                raise ValidationError("claim subject_ref, metric_or_aspect, period, basis and normalized_statement are required")
            producer_refs = wire["producer_invocation_refs"]
            if not isinstance(producer_refs, list) or not producer_refs or not all(isinstance(x, str) and x for x in producer_refs):
                raise ValidationError("claim producer_invocation_refs must be a non-empty list of invocation ids")
            for producer_ref in producer_refs:
                if not cur.execute("SELECT 1 FROM model_invocations WHERE invocation_id=?", (producer_ref,)).fetchone():
                    raise NotFound(f"claim producer invocation not found: {producer_ref}")
            wire["content_hash"] = self._ledger_hash(wire)
            if data.get("content_hash") is not None and data["content_hash"] != wire["content_hash"]:
                raise ValidationError("claim content_hash does not match canonical version")
            try:
                validated = ClaimVersion.from_dict(wire)
            except Exception as exc:
                raise ValidationError(str(exc)) from exc
            encoded = canonical_json(validated.to_dict())
            try:
                cur.execute("INSERT INTO claim_versions(claim_version_id,claim_ref,version_number,claim_json,content_hash,prior_version_id,created_at) VALUES(?,?,?,?,?,?,?)", (validated.id, validated.claim_ref, validated.version, encoded, validated.content_hash, validated.prior_version_ref, validated.created_at))
            except sqlite3.IntegrityError as exc:
                raise IdempotencyConflict("claim version id or version number already exists") from exc
            event_id = self._insert_event(cur, "claim_versioned", "claim", validated.claim_ref, validated.to_dict(), content_hash=validated.content_hash, actor_id=actor_ref or data.get("actor_ref", "system:dalton-core"), idempotency_key=f"claim:{validated.id}")
            self._emit_numeric_challenges(cur, validated)
            return {"claim_ref": validated.claim_ref, "claim_version_id": validated.id, "version": validated.version, "content_hash": validated.content_hash, "event_id": event_id, "status": self._claim_status(cur, validated.id)}

    add_claim = register_claim
    create_claim = register_claim
    create_claim_version = register_claim

    def relate_evidence(
        self,
        relation: Mapping[str, Any] | Any = None,
        *,
        relation_id: str | None = None,
        idempotency_key: str | None = None,
        actor_ref: str | None = None,
    ) -> dict[str, Any]:
        data = self._ledger_payload(relation)
        rid = relation_id or data.get("id")
        if not isinstance(rid, str) or not rid:
            raise ValidationError("relation_id is required; use an explicit stable relation id")
        if idempotency_key is not None and (not isinstance(idempotency_key, str) or not idempotency_key):
            raise ValidationError("idempotency_key must be a non-empty string")
        with self._transaction() as cur:
            evidence_version_id = data.get("evidence_version_ref", data.get("evidence_version_id"))
            claim_version_id = data.get("claim_version_ref", data.get("claim_version_id"))
            evidence = cur.execute("SELECT evidence_json FROM evidence_versions WHERE evidence_version_id=?", (evidence_version_id,)).fetchone()
            claim = cur.execute("SELECT claim_json FROM claim_versions WHERE claim_version_id=?", (claim_version_id,)).fetchone()
            if not evidence or not claim:
                raise NotFound("evidence_version_ref and claim_version_ref must exist")
            e_doc = json.loads(evidence[0]); c_doc = json.loads(claim[0])
            if data.get("evidence_ref", e_doc["evidence_ref"]) != e_doc["evidence_ref"] or data.get("claim_ref", c_doc["claim_ref"]) != c_doc["claim_ref"]:
                raise ValidationError("relation stable refs do not match version refs")
            if "source_lineage" in data and list(data["source_lineage"]) != list(e_doc["source_lineage"]):
                raise ValidationError("relation source_lineage must exactly inherit its evidence")
            if "independence_group" in data and data["independence_group"] != e_doc["independence_group"]:
                raise ValidationError("relation independence_group must exactly inherit its evidence")
            wire = {
                "schema_version": data.get("schema_version", "0.1"), "id": rid, "created_at": data.get("created_at") or _now(),
                "evidence_ref": e_doc["evidence_ref"], "evidence_version_ref": e_doc["id"], "claim_ref": c_doc["claim_ref"], "claim_version_ref": c_doc["id"],
                "relation": _scalar(data.get("relation")), "source_lineage": list(data.get("source_lineage", e_doc["source_lineage"])),
                "independence_group": data.get("independence_group", e_doc["independence_group"]), "actor_ref": actor_ref or data.get("actor_ref", "system:dalton-core"),
            }
            try:
                wire["content_hash"] = self._ledger_hash(wire)
                validated = EvidenceRelation.from_dict(wire)
            except Exception as exc:
                raise ValidationError(str(exc)) from exc
            request_hash = content_hash({
                "relation_id": validated.id,
                "evidence_version_ref": validated.evidence_version_ref,
                "claim_version_ref": validated.claim_version_ref,
                "relation": validated.relation.value,
                "source_lineage": list(validated.source_lineage),
                "independence_group": validated.independence_group,
            })
            if idempotency_key is not None:
                prior_key = cur.execute("SELECT * FROM relation_idempotency_keys WHERE idempotency_key=?", (idempotency_key,)).fetchone()
                if prior_key:
                    if prior_key["request_hash"] == request_hash:
                        original = json.loads(prior_key["result_json"])
                        return {**original, "status": "duplicate", "idempotency_key": idempotency_key}
                    return {"status": "conflict", "idempotency_key": idempotency_key, "existing_request_hash": prior_key["request_hash"], "request_hash": request_hash}
            if cur.execute("SELECT 1 FROM evidence_relations WHERE relation_id=?", (validated.id,)).fetchone():
                raise IdempotencyConflict(f"relation_id {validated.id!r} already exists")
            encoded = canonical_json(validated.to_dict())
            try:
                cur.execute("INSERT INTO evidence_relations(relation_id,evidence_ref,evidence_version_id,claim_ref,claim_version_id,relation,relation_json,content_hash,created_at) VALUES(?,?,?,?,?,?,?,?,?)", (validated.id, validated.evidence_ref, validated.evidence_version_ref, validated.claim_ref, validated.claim_version_ref, validated.relation.value, encoded, validated.content_hash, validated.created_at))
            except sqlite3.IntegrityError as exc:
                raise IdempotencyConflict("evidence relation id already exists") from exc
            event_id = self._insert_event(cur, "evidence_related", "claim", validated.claim_ref, validated.to_dict(), content_hash=validated.content_hash, actor_id=validated.actor_ref, idempotency_key=f"relation:{validated.id}")
            result = {"status": "fresh", "relation_id": validated.id, "claim_ref": validated.claim_ref, "claim_version_ref": validated.claim_version_ref, "event_id": event_id, "content_hash": validated.content_hash}
            if idempotency_key is not None:
                cur.execute("INSERT INTO relation_idempotency_keys(idempotency_key,request_hash,result_json,relation_id,created_at) VALUES(?,?,?,?,?)", (idempotency_key, request_hash, canonical_json(result), validated.id, validated.created_at))
                result["idempotency_key"] = idempotency_key
            return result

    add_evidence_relation = relate_evidence
    create_evidence_relation = relate_evidence

    def adjudicate_claim(
        self,
        adjudication: Mapping[str, Any] | Any = None,
        *,
        adjudication_version_id: str | None = None,
        adjudicator_invocation: Any = None,
        subject_invocation_refs: Any = None,
        actor_ref: str | None = None,
    ) -> dict[str, Any]:
        data = self._ledger_payload(adjudication)
        adjudicator = adjudicator_invocation if adjudicator_invocation is not None else data.get("adjudicator_invocation")
        adjudicator_id = data.get("adjudicator_invocation_ref") or data.get("adjudicator_invocation_id")
        if adjudicator is not None:
            if isinstance(adjudicator, Mapping):
                extra = set(adjudicator) - {"id", "invocation_id"}
                if extra:
                    raise ValidationError("adjudicator_invocation must be an existing invocation reference, not an inline payload")
                adjudicator_id = adjudicator.get("invocation_id", adjudicator.get("id"))
            else:
                adjudicator_id = str(adjudicator)
        subjects = subject_invocation_refs if subject_invocation_refs is not None else data.get("subject_invocation_refs", data.get("subject_invocation_ref"))
        claim_version_id = data.get("claim_version_ref", data.get("claim_version_id"))
        claim_ref = data.get("claim_ref")
        if not claim_version_id:
            raise ValidationError("claim_version_ref is required")
        if not adjudicator_id:
            raise ValidationError("adjudicator invocation is required")
        with self._transaction() as cur:
            claim_row = cur.execute("SELECT claim_json FROM claim_versions WHERE claim_version_id=?", (claim_version_id,)).fetchone()
            if not claim_row:
                raise NotFound(f"claim version not found: {claim_version_id}")
            claim_doc = json.loads(claim_row[0]); claim_ref = claim_ref or claim_doc["claim_ref"]
            if claim_doc["claim_ref"] != claim_ref:
                raise ValidationError("claim_ref does not match claim_version_ref")
            derived_subjects = list(
                claim_doc.get("producer_invocation_refs")
                or claim_doc.get("producer_execution_refs")
                or []
            )
            if subjects is None:
                subjects = list(derived_subjects)
            elif isinstance(subjects, str):
                subjects = [subjects]
            else:
                subjects = list(subjects)
                if subjects != derived_subjects:
                    raise ValidationError("subject_invocation_refs must exactly match ClaimVersion producer_invocation_refs")
            if not subjects:
                raise ValidationError("claim has no producer invocation refs")
            adjudicator_row = cur.execute("SELECT * FROM model_invocations WHERE invocation_id=?", (str(adjudicator_id),)).fetchone()
            if not adjudicator_row:
                raise NotFound(f"adjudicator invocation not found: {adjudicator_id}")
            subject_rows = []
            for subject_id in subjects:
                row = cur.execute("SELECT * FROM model_invocations WHERE invocation_id=?", (str(subject_id),)).fetchone()
                if not row:
                    execution = cur.execute(
                        "SELECT kind FROM execution_invocations WHERE execution_id=?",
                        (str(subject_id),),
                    ).fetchone()
                    if execution is not None:
                        raise VerificationRequired(
                            "non-model producer execution requires a dedicated adjudication policy"
                        )
                    raise NotFound(f"claim producer invocation not found: {subject_id}")
                subject_rows.append(row)
            policy = cur.execute("SELECT v.policy_version_id,v.policy_json,v.effective_from,v.effective_until FROM governance_policy_pointer p JOIN governance_policy_versions v ON v.policy_version_id=p.policy_version_id WHERE p.pointer_id=1").fetchone()
            if not policy:
                raise GateRejected("no active governance policy")
            self._assert_policy_effective(policy)
            policy_doc = json.loads(policy[1])
            for subject in subject_rows:
                ok, reason = evaluate_gate(policy_doc, "pass", dict(subject), dict(adjudicator_row))
                if not ok:
                    raise IndependenceViolation(reason or "adjudication independence failed")
            latest = cur.execute("SELECT * FROM adjudication_versions WHERE claim_ref=? ORDER BY version_number DESC LIMIT 1", (claim_ref,)).fetchone()
            prior = latest["adjudication_version_id"] if latest else None
            if data.get("prior_version_ref") is not None and data["prior_version_ref"] != prior:
                raise ValidationError("adjudication prior_version_ref does not match current version")
            version = int(latest["version_number"]) + 1 if latest else 1
            supplied_version = data.get("version")
            if supplied_version is not None:
                try:
                    supplied_version_int = int(supplied_version)
                except (TypeError, ValueError) as exc:
                    raise ValidationError("adjudication version must be an integer") from exc
                if isinstance(supplied_version, bool) or supplied_version_int != version:
                    raise ValidationError(f"adjudication version must be the next chain version ({version})")
            status = _scalar(data.get("adjudicated_status", data.get("status")))
            if status not in {x.value for x in AdjudicatedStatus}:
                raise ValidationError("adjudicated_status must be a valid adjudication status")
            wire = {
                "schema_version": data.get("schema_version", "0.1"), "id": adjudication_version_id or data.get("id") or uuid.uuid4().hex,
                "created_at": data.get("created_at") or _now(), "claim_ref": claim_ref, "claim_version_ref": claim_version_id, "version": version,
                "adjudicated_status": status, "rationale": data.get("rationale"), "findings": list(data.get("findings", [])),
                "adjudicator_invocation_ref": adjudicator_row["invocation_id"], "subject_invocation_refs": [x["invocation_id"] for x in subject_rows],
                "independence_policy_ref": policy[0], "prior_version_ref": prior,
            }
            if not isinstance(wire["rationale"], str) or not wire["rationale"]:
                raise ValidationError("rationale is required")
            wire["content_hash"] = self._ledger_hash(wire)
            validated = AdjudicationVersion.from_dict(wire)
            encoded = canonical_json(validated.to_dict())
            try:
                cur.execute("INSERT INTO adjudication_versions(adjudication_version_id,claim_ref,claim_version_id,version_number,adjudicated_status,adjudication_json,content_hash,prior_version_id,adjudicator_invocation_id,independence_policy_id,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)", (validated.id, validated.claim_ref, validated.claim_version_ref, validated.version, validated.adjudicated_status.value, encoded, validated.content_hash, validated.prior_version_ref, validated.adjudicator_invocation_ref, validated.independence_policy_ref, validated.created_at))
            except sqlite3.IntegrityError as exc:
                raise IdempotencyConflict("adjudication version id already exists") from exc
            event_id = self._insert_event(cur, "claim_adjudicated", "claim", claim_ref, validated.to_dict(), content_hash=validated.content_hash, actor_id=actor_ref or data.get("actor_ref", "system:dalton-core"), idempotency_key=f"adjudication:{validated.id}")
            return {"adjudication_version_id": validated.id, "claim_ref": claim_ref, "claim_version_ref": claim_version_id, "status": status, "event_id": event_id}

    add_adjudication = adjudicate_claim
    create_adjudication_version = adjudicate_claim

    @staticmethod
    def _claim_semantic_key(
        document: Mapping[str, Any],
    ) -> tuple[str, ...]:
        """Return an exact numeric-comparison key across Ledger versions."""
        base = (
            str(document["subject_ref"]),
            str(document["metric_or_aspect"]),
            canonical_json(document["period"]),
            str(document["basis"]),
            str(document["unit"]),
        )
        if document.get("schema_version") == "0.2":
            return (*base, str(document.get("currency")), str(document.get("scale")))
        return base

    @staticmethod
    def _claim_values_equal(left: Any, right: Any) -> bool:
        """Compare 0.1 JSON numbers and 0.2 canonical Decimal text exactly."""
        try:
            return Decimal(str(left)) == Decimal(str(right))
        except (InvalidOperation, ValueError):
            return left == right

    def _latest_claim_rows(
        self,
        cur: sqlite3.Cursor,
        *,
        key: tuple[str, ...] | None = None,
    ) -> list[sqlite3.Row]:
        rows = cur.execute("SELECT c.* FROM claim_versions c JOIN (SELECT claim_ref,MAX(version_number) version_number FROM claim_versions GROUP BY claim_ref) latest ON latest.claim_ref=c.claim_ref AND latest.version_number=c.version_number").fetchall()
        if key is None:
            return rows
        return [
            row for row in rows
            if self._claim_semantic_key(json.loads(row["claim_json"])) == key
        ]

    def _emit_numeric_challenges(self, cur: sqlite3.Cursor, claim: ClaimVersion) -> None:
        if claim.claim_kind != ClaimKind.QUANTITATIVE:
            return
        self._emit_numeric_challenges_document(cur, claim.to_dict())

    def _emit_numeric_challenges_document(
        self, cur: sqlite3.Cursor, claim: Mapping[str, Any]
    ) -> None:
        if claim.get("claim_kind") != ClaimKind.QUANTITATIVE.value:
            return
        key = self._claim_semantic_key(claim)
        for other in self._latest_claim_rows(cur, key=key):
            if other["claim_version_id"] == claim["id"]:
                continue
            other_doc = json.loads(other["claim_json"])
            if self._claim_values_equal(other_doc.get("value"), claim.get("value")):
                continue
            ids = sorted((str(claim["id"]), other["claim_version_id"]))
            conflict_key = "numeric:" + ":".join(ids)
            exists = cur.execute("SELECT 1 FROM claim_challenges WHERE conflict_key=?", (conflict_key,)).fetchone()
            if exists:
                continue
            challenge = {"challenge_id": uuid.uuid4().hex, "conflict_key": conflict_key, "claim_version_id": claim["id"], "conflicting_claim_version_id": other["claim_version_id"], "reason": "exact numeric claims conflict", "semantic_key": list(key), "values": [claim.get("value"), other_doc.get("value")]}
            encoded = canonical_json(challenge)
            cur.execute("INSERT INTO claim_challenges(challenge_id,conflict_key,claim_version_id,conflicting_claim_version_id,challenge_json,content_hash,created_at) VALUES(?,?,?,?,?,?,?)", (challenge["challenge_id"], conflict_key, claim["id"], other["claim_version_id"], encoded, content_hash(challenge), _now()))
            self._insert_event(cur, "claim_challenged", "claim", str(claim["claim_ref"]), challenge, content_hash=content_hash(challenge), actor_id="system:dalton-core", idempotency_key=f"challenge:{conflict_key}")

    def _claim_status(self, cur: sqlite3.Cursor, claim_version_id: str) -> str:
        row = cur.execute(
            "SELECT claim_ref,version_number,claim_json FROM claim_versions "
            "WHERE claim_version_id=?", (claim_version_id,),
        ).fetchone()
        if row is None:
            raise NotFound(f"claim version not found: {claim_version_id}")
        latest = cur.execute(
            "SELECT claim_version_id FROM claim_versions WHERE claim_ref=? "
            "ORDER BY version_number DESC,claim_version_id DESC LIMIT 1",
            (row["claim_ref"],),
        ).fetchone()
        if latest is None or latest["claim_version_id"] != claim_version_id:
            return AdjudicatedStatus.SUPERSEDED.value
        document = json.loads(row["claim_json"])
        if document.get("claim_kind") == ClaimKind.QUANTITATIVE.value:
            key = self._claim_semantic_key(document)
            for other in self._latest_claim_rows(cur, key=key):
                if other["claim_version_id"] == claim_version_id:
                    continue
                other_document = json.loads(other["claim_json"])
                if not self._claim_values_equal(
                    other_document.get("value"), document.get("value")
                ):
                    return AdjudicatedStatus.CONTESTED.value
        adjudication = cur.execute(
            "SELECT adjudicated_status FROM adjudication_versions "
            "WHERE claim_version_id=? "
            "ORDER BY version_number DESC,adjudication_version_id DESC LIMIT 1",
            (claim_version_id,),
        ).fetchone()
        return adjudication[0] if adjudication is not None else "proposed"

    @classmethod
    def project_claim_status(
        cls, snapshot: Mapping[str, Any], claim_version_id: str
    ) -> str:
        """Project one exact ClaimVersion status from an immutable snapshot.

        The order is deliberately part of the Ledger authority contract:
        superseded versions cannot inherit a later version's adjudication;
        deterministic numeric conflicts win over adjudication; otherwise only
        an adjudication attached to this exact ClaimVersion applies.
        """
        return cls.project_claim_status_details(snapshot, claim_version_id)["status"]

    @classmethod
    def project_claim_status_details(
        cls, snapshot: Mapping[str, Any], claim_version_id: str
    ) -> dict[str, str]:
        """Return status and the newest immutable authority timestamp used."""
        rows = list(snapshot.get("claim_versions", []))
        target = next(
            (row for row in rows if row.get("claim_version_id") == claim_version_id),
            None,
        )
        if target is None:
            raise NotFound(f"claim version not found: {claim_version_id}")
        authority_times = [str(target["created_at"])]

        def projected(status: str) -> dict[str, str]:
            latest_time = max(
                _parse_rfc3339(value, "claim status authority timestamp")
                for value in authority_times
            )
            return {
                "status": status,
                "updated_at": latest_time.isoformat(timespec="microseconds"),
            }

        latest_refs = dict(snapshot.get("latest_claim_version_refs", {}))
        if latest_refs.get(target["claim_ref"]) != claim_version_id:
            replacement = next(
                (
                    row for row in rows
                    if row.get("claim_version_id") == latest_refs.get(target["claim_ref"])
                ),
                None,
            )
            if replacement is not None:
                authority_times.append(str(replacement["created_at"]))
            return projected(AdjudicatedStatus.SUPERSEDED.value)
        document = target["claim"]
        if document.get("claim_kind") == ClaimKind.QUANTITATIVE.value:
            key = cls._claim_semantic_key(document)
            has_conflict = False
            for other in rows:
                if other["claim_version_id"] == claim_version_id:
                    continue
                if latest_refs.get(other["claim_ref"]) != other["claim_version_id"]:
                    continue
                other_document = other["claim"]
                if (
                    cls._claim_semantic_key(other_document) == key
                    and not cls._claim_values_equal(
                        other_document.get("value"), document.get("value")
                    )
                ):
                    authority_times.append(str(other["created_at"]))
                    has_conflict = True
            if has_conflict:
                return projected(AdjudicatedStatus.CONTESTED.value)
        adjudications = [
            item for item in snapshot.get("latest_adjudications", [])
            if item.get("claim_version_ref") == claim_version_id
        ]
        if adjudications:
            authority_times.append(str(adjudications[0]["created_at"]))
            return projected(
                str(adjudications[0]["adjudication"]["adjudicated_status"])
            )
        return projected("proposed")

    def claim_index_snapshot(
        self, *, created_at: str | None = None
    ) -> dict[str, Any]:
        """Read one consistent, status-free Ledger snapshot for ClaimIndex.

        The returned snapshot contains every immutable ClaimVersion, its
        relations/challenges, and the latest adjudication for each exact claim
        version.  It is read under one SQLite snapshot and carries a Core-
        derived ref/hash; callers cannot provide either authority value.
        """
        if self.connection.in_transaction:
            raise RuntimeError("claim_index_snapshot cannot run inside a transaction")

        def snapshot_timestamp(value: str) -> str:
            return _parse_rfc3339(value, "claim index snapshot timestamp").isoformat(
                timespec="microseconds"
            )

        self.connection.execute("BEGIN")
        try:
            claim_rows = self.connection.execute(
                "SELECT claim_version_id,claim_ref,version_number,claim_json,content_hash,created_at "
                "FROM claim_versions ORDER BY claim_version_id"
            ).fetchall()
            claims = [
                {
                    "claim_version_id": row["claim_version_id"],
                    "claim_ref": row["claim_ref"],
                    "version": row["version_number"],
                    "content_hash": row["content_hash"],
                    "created_at": snapshot_timestamp(row["created_at"]),
                    "claim": json.loads(row["claim_json"]),
                }
                for row in claim_rows
            ]
            latest_claim_rows: dict[str, dict[str, Any]] = {}
            for row in claims:
                existing = latest_claim_rows.get(row["claim_ref"])
                if existing is None:
                    latest_claim_rows[row["claim_ref"]] = row
                    continue
                if (row["version"], row["claim_version_id"]) > (
                    existing["version"], existing["claim_version_id"]
                ):
                    latest_claim_rows[row["claim_ref"]] = row
            latest_claims = {
                claim_ref: row["claim_version_id"]
                for claim_ref, row in latest_claim_rows.items()
            }

            relations = [
                json.loads(row[0])
                for row in self.connection.execute(
                    "SELECT relation_json FROM evidence_relations ORDER BY relation_id"
                ).fetchall()
            ]
            challenges = [
                json.loads(row[0])
                for row in self.connection.execute(
                    "SELECT challenge_json FROM claim_challenges ORDER BY conflict_key,challenge_id"
                ).fetchall()
            ]
            adjudication_rows = self.connection.execute(
                "SELECT adjudication_version_id,claim_ref,claim_version_id,version_number,"
                "adjudication_json,content_hash,created_at FROM adjudication_versions "
                "ORDER BY claim_version_id,version_number DESC,adjudication_version_id DESC"
            ).fetchall()
            latest_adjudications: dict[str, dict[str, Any]] = {}
            for row in adjudication_rows:
                latest_adjudications.setdefault(
                    row["claim_version_id"],
                    {
                        "adjudication_version_id": row["adjudication_version_id"],
                        "claim_ref": row["claim_ref"],
                        "claim_version_ref": row["claim_version_id"],
                        "version": row["version_number"],
                        "adjudication": json.loads(row["adjudication_json"]),
                        "content_hash": row["content_hash"],
                        "created_at": snapshot_timestamp(row["created_at"]),
                    },
                )
            base = {
                "schema_version": "0.1",
                "created_at": snapshot_timestamp(created_at or _now()),
                "claim_versions": claims,
                "latest_claim_version_refs": latest_claims,
                "evidence_relations": relations,
                "claim_challenges": challenges,
                "latest_adjudications": list(latest_adjudications.values()),
            }
            identity_hash = content_hash(base)
            snapshot = {
                **base,
                "id": f"ledger-snapshot:claim-index:{identity_hash}",
            }
            snapshot["content_hash"] = content_hash(snapshot)
            self.connection.commit()
            return snapshot
        except BaseException:
            self.connection.rollback()
            raise

    def get_claim(self, claim_version_id: str) -> dict[str, Any] | None:
        row = self.connection.execute("SELECT * FROM claim_versions WHERE claim_version_id=?", (claim_version_id,)).fetchone()
        if not row:
            return None
        result = dict(row); result["claim"] = json.loads(result.pop("claim_json"))
        relation_rows = self.connection.execute("SELECT relation_json FROM evidence_relations WHERE claim_version_id=? ORDER BY created_at,relation_id", (claim_version_id,)).fetchall()
        result["evidence_relations"] = [json.loads(r[0]) for r in relation_rows]
        result["status"] = self._claim_status(self.connection.cursor(), claim_version_id)
        return result

    claim_projection = get_claim

    def list_claim_challenges(self) -> list[dict[str, Any]]:
        rows = self.connection.execute("SELECT * FROM claim_challenges ORDER BY created_at,challenge_id").fetchall()
        return [dict(r) | {"challenge": json.loads(r["challenge_json"])} for r in rows]

    def list_events(self, *, aggregate_id: str | None = None) -> list[dict[str, Any]]:
        if aggregate_id is None:
            rows = self.connection.execute("SELECT * FROM domain_events ORDER BY created_at,event_id").fetchall()
        else:
            rows = self.connection.execute("SELECT * FROM domain_events WHERE aggregate_id=? ORDER BY created_at,event_id", (aggregate_id,)).fetchall()
        return [dict(row) for row in rows]


# Friendly names for adapters that do not want to couple themselves to the
# concrete implementation name.
Store = DaltonStore
SQLiteStore = DaltonStore
