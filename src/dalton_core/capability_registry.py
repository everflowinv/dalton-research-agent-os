"""Append-only proposal registry for self-authored Dalton capabilities.

This module deliberately does *not* execute submitted capability code.  A
builder may submit a content/artifact hash and an external sandbox may submit
evaluation evidence.  Only the trusted Core writer can persist those records,
and a reusable capability becomes active only after a human decision.
"""

from __future__ import annotations

import json
import math
import re
import sqlite3
import uuid
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .contracts import CapabilityProposal
from .policy import check_independence
from .store import canonical_json, content_hash


class CapabilityRegistryError(Exception):
    """Base error for capability registry operations."""


class CapabilityConflict(CapabilityRegistryError):
    """An immutable identifier or idempotency key was reused differently."""


class CapabilityNotFound(CapabilityRegistryError):
    pass


class EvaluationRejected(CapabilityRegistryError):
    pass


class PromotionRejected(CapabilityRegistryError):
    pass


class PermissionEscalation(PromotionRejected):
    pass


_SCHEMA_PATH = Path(__file__).with_name("capability_schema.sql")
_SCHEMA_VERSION = "0.1"
_DECISIONS = frozenset({"approve", "reject", "rollback"})
_HUMAN_PREFIXES = ("human:",)
_BUILDER_KEYS = ("builder_invocation_ref", "builder_invocation_id", "builder")
_HUMAN_ACTOR_RE = re.compile(r"^human:[A-Za-z0-9][A-Za-z0-9._/@:-]*$")
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


def _sha256(value: Any, name: str) -> str:
    if not isinstance(value, str) or not _SHA256_RE.fullmatch(value):
        raise CapabilityRegistryError(
            f"{name} must be 64 lowercase SHA-256 hex characters"
        )
    return value


def _wire_fields(data: Mapping[str, Any], required: set[str], optional: set[str], name: str) -> dict[str, Any]:
    if not isinstance(data, Mapping):
        raise CapabilityRegistryError(f"{name} must be an object")
    unknown = set(data) - required - optional
    missing = required - set(data)
    if unknown:
        raise CapabilityRegistryError(f"{name}: unknown field(s): {sorted(unknown)}")
    if missing:
        raise CapabilityRegistryError(f"{name}: missing field(s): {sorted(missing)}")
    result = dict(data)
    if result.get("schema_version") != _SCHEMA_VERSION:
        raise CapabilityRegistryError(f"{name}.schema_version must be {_SCHEMA_VERSION!r}")
    for field in ("id", "created_at", "content_hash"):
        if not isinstance(result.get(field), str) or not result[field]:
            raise CapabilityRegistryError(f"{name}.{field} must be a non-empty string")
    expected_hash = content_hash({k: v for k, v in result.items() if k != "content_hash"})
    if result["content_hash"] != expected_hash:
        raise CapabilityRegistryError(f"{name}.content_hash does not match canonical wire")
    return result


@dataclass(frozen=True, slots=True)
class CapabilityVersion:
    schema_version: str
    id: str
    created_at: str
    capability_version_ref: str
    version: int
    proposal: Mapping[str, Any]
    content_hash: str
    artifact_hash: str
    prior_version_ref: str | None
    builder_invocation_ref: str | None
    actor_ref: str

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "CapabilityVersion":
        obj = _wire_fields(data, {"schema_version", "id", "created_at", "capability_version_ref", "version", "proposal", "content_hash", "artifact_hash", "prior_version_ref", "builder_invocation_ref", "actor_ref"}, set(), "CapabilityVersion")
        for field in ("capability_version_ref", "actor_ref"):
            _nonempty(obj[field], f"CapabilityVersion.{field}")
        _sha256(obj["artifact_hash"], "CapabilityVersion.artifact_hash")
        if isinstance(obj["version"], bool) or not isinstance(obj["version"], int) or obj["version"] < 1:
            raise CapabilityRegistryError("CapabilityVersion.version must be a positive integer")
        if not isinstance(obj["proposal"], Mapping):
            raise CapabilityRegistryError("CapabilityVersion.proposal must be an object")
        for field in ("prior_version_ref", "builder_invocation_ref"):
            if obj[field] is not None:
                _nonempty(obj[field], f"CapabilityVersion.{field}")
        return cls(**obj)

    def to_dict(self) -> dict[str, Any]:
        return {"schema_version": self.schema_version, "id": self.id, "created_at": self.created_at,
                "capability_version_ref": self.capability_version_ref, "version": self.version,
                "proposal": dict(self.proposal), "content_hash": self.content_hash,
                "artifact_hash": self.artifact_hash, "prior_version_ref": self.prior_version_ref,
                "builder_invocation_ref": self.builder_invocation_ref, "actor_ref": self.actor_ref}


@dataclass(frozen=True, slots=True)
class CapabilityEvaluation:
    schema_version: str
    id: str
    created_at: str
    capability_version_ref: str
    proposal_content_hash: str
    fixtures: tuple[str, ...]
    baseline: Mapping[str, Any]
    results: Mapping[str, Any]
    environment_hash: str
    evaluator_invocation_ref: str
    builder_invocation_ref: str | None
    policy_version_id: str
    policy_content_hash: str
    actor_ref: str
    content_hash: str

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "CapabilityEvaluation":
        obj = _wire_fields(data, {"schema_version", "id", "created_at", "capability_version_ref", "proposal_content_hash", "fixtures", "baseline", "results", "environment_hash", "evaluator_invocation_ref", "builder_invocation_ref", "policy_version_id", "policy_content_hash", "actor_ref", "content_hash"}, set(), "CapabilityEvaluation")
        for field in ("capability_version_ref", "evaluator_invocation_ref", "policy_version_id", "actor_ref"):
            _nonempty(obj[field], f"CapabilityEvaluation.{field}")
        for field in ("proposal_content_hash", "environment_hash", "policy_content_hash"):
            _sha256(obj[field], f"CapabilityEvaluation.{field}")
        obj["fixtures"] = _validate_fixtures(obj["fixtures"], "CapabilityEvaluation.fixtures")
        for field in ("baseline", "results"):
            if not isinstance(obj[field], Mapping):
                raise CapabilityRegistryError(f"CapabilityEvaluation.{field} must be an object")
        if obj["builder_invocation_ref"] is not None:
            _nonempty(obj["builder_invocation_ref"], "CapabilityEvaluation.builder_invocation_ref")
        obj["fixtures"] = tuple(obj["fixtures"])
        return cls(**obj)

    def to_dict(self) -> dict[str, Any]:
        result = {"schema_version": self.schema_version, "id": self.id, "created_at": self.created_at,
                  "capability_version_ref": self.capability_version_ref, "proposal_content_hash": self.proposal_content_hash,
                  "fixtures": list(self.fixtures), "baseline": dict(self.baseline), "results": dict(self.results),
                  "environment_hash": self.environment_hash, "evaluator_invocation_ref": self.evaluator_invocation_ref,
                  "builder_invocation_ref": self.builder_invocation_ref, "policy_version_id": self.policy_version_id,
                  "policy_content_hash": self.policy_content_hash, "actor_ref": self.actor_ref,
                  "content_hash": self.content_hash}
        return result


@dataclass(frozen=True, slots=True)
class CapabilityDecision:
    schema_version: str
    id: str
    created_at: str
    capability_version_ref: str
    evaluation_ref: str | None
    decision: str
    actor_ref: str
    requested_permissions: Mapping[str, Any]
    rationale: str
    rollback_to_version_ref: str | None
    content_hash: str

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "CapabilityDecision":
        obj = _wire_fields(data, {"schema_version", "id", "created_at", "capability_version_ref", "evaluation_ref", "decision", "actor_ref", "requested_permissions", "rationale", "rollback_to_version_ref", "content_hash"}, set(), "CapabilityDecision")
        _nonempty(obj["capability_version_ref"], "CapabilityDecision.capability_version_ref")
        _nonempty(obj["actor_ref"], "CapabilityDecision.actor_ref")
        if obj["decision"] not in _DECISIONS:
            raise CapabilityRegistryError("CapabilityDecision.decision is invalid")
        if not isinstance(obj["requested_permissions"], Mapping):
            raise CapabilityRegistryError("CapabilityDecision.requested_permissions must be an object")
        if not isinstance(obj["rationale"], str):
            raise CapabilityRegistryError("CapabilityDecision.rationale must be a string")
        for field in ("evaluation_ref", "rollback_to_version_ref"):
            if obj[field] is not None:
                _nonempty(obj[field], f"CapabilityDecision.{field}")
        return cls(**obj)

    def to_dict(self) -> dict[str, Any]:
        return {"schema_version": self.schema_version, "id": self.id, "created_at": self.created_at,
                "capability_version_ref": self.capability_version_ref, "evaluation_ref": self.evaluation_ref,
                "decision": self.decision, "actor_ref": self.actor_ref,
                "requested_permissions": dict(self.requested_permissions), "rationale": self.rationale,
                "rollback_to_version_ref": self.rollback_to_version_ref, "content_hash": self.content_hash}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _policy_time(value: Any, name: str) -> datetime:
    if not isinstance(value, str):
        raise PromotionRejected(f"governance policy {name} is not RFC3339")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise PromotionRejected(f"governance policy {name} is not RFC3339") from exc
    if parsed.tzinfo is None:
        raise PromotionRejected(f"governance policy {name} must include timezone")
    return parsed.astimezone(timezone.utc)


def _id(value: Any = None) -> str:
    value = uuid.uuid4().hex if value is None else str(value)
    if not value:
        raise CapabilityRegistryError("identifier must be non-empty")
    return value


def _mapping(value: Any, name: str) -> dict[str, Any]:
    if hasattr(value, "to_dict"):
        value = value.to_dict()
    elif hasattr(value, "__dataclass_fields__"):
        # Existing CapabilityProposal is expected to expose to_dict; this is
        # only a defensive fallback for compatible wire objects.
        import dataclasses

        value = dataclasses.asdict(value)
    if not isinstance(value, Mapping):
        raise CapabilityRegistryError(f"{name} must be an object")
    return dict(value)


def _json_value(value: Any, name: str) -> Any:
    try:
        encoded = canonical_json(value)
        return json.loads(encoded)
    except (TypeError, ValueError) as exc:
        raise CapabilityRegistryError(f"{name} must be JSON-serializable") from exc


def _nonempty(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise CapabilityRegistryError(f"{name} must be a non-empty string")
    return value


def _is_human(actor_ref: str) -> bool:
    # This prefix is only a trusted-Core actor-record convention.  It is not
    # authentication.  The writer must authenticate a human principal before
    # it calls the promotion operation; the registry merely enforces the
    # resulting policy at the storage boundary.
    return isinstance(actor_ref, str) and bool(_HUMAN_ACTOR_RE.fullmatch(actor_ref))


def _validate_fixtures(value: Any, name: str) -> list[str]:
    if not isinstance(value, (list, tuple)) or not value:
        raise CapabilityRegistryError(f"{name} must be a non-empty array")
    if not all(isinstance(x, str) and x for x in value):
        raise CapabilityRegistryError(f"{name} must contain non-empty strings")
    result = list(value)
    if len(result) != len(set(result)):
        raise CapabilityRegistryError(f"{name} must not contain duplicates")
    return result


def _validate_permission_document(value: Any, path: str = "permissions") -> Any:
    """Validate a monotonic permission document; null is never meaningful."""
    if value is None:
        raise PermissionEscalation(f"{path} cannot be null")
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        if not value:
            raise PermissionEscalation(f"{path} string cannot be empty")
        return value
    if isinstance(value, (int, float)):
        if isinstance(value, float) and not math.isfinite(value):
            raise PermissionEscalation(f"{path} number must be finite")
        return value
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str) or not key:
                raise PermissionEscalation(f"{path} keys must be non-empty strings")
            _validate_permission_document(item, f"{path}.{key}")
        return value
    if isinstance(value, (list, tuple, set)):
        for index, item in enumerate(value):
            _validate_permission_document(item, f"{path}[{index}]")
        return value
    raise PermissionEscalation(f"{path} contains an unsupported value type")


def _permission_subset(requested: Any, allowed: Any, path: str = "permissions") -> bool:
    """Return whether requested permissions are contained in the proposal.

    Permission documents are intentionally generic at this layer.  Scalar
    booleans, lists and nested objects get monotonic subset semantics; unknown
    scalar values must match exactly.  This prevents a new permission spelling
    from silently becoming an escalation.
    """

    if isinstance(requested, Mapping):
        if not isinstance(allowed, Mapping):
            return False
        return all(k in allowed and _permission_subset(v, allowed[k], f"{path}.{k}") for k, v in requested.items())
    if isinstance(requested, (list, tuple, set)):
        if not isinstance(allowed, (list, tuple, set)):
            return False
        try:
            return set(requested).issubset(set(allowed))
        except TypeError:
            return all(any(_permission_subset(item, candidate, path) for candidate in allowed) for item in requested)
    if isinstance(requested, bool):
        return requested is False or allowed is True
    if requested is None:
        return True
    return requested == allowed


class CapabilityRegistry:
    """Capability proposal/evaluation/promotion registry on a DaltonStore.

    ``store`` owns the SQLite connection and transaction boundary.  This
    class only adds namespaced tables to that connection and never opens a
    second database.  Callers outside trusted Core should use a writer service
    and must not receive the database path.  The ``human:`` actor prefix is
    only an internal record convention, not authentication; the writer must
    authenticate a human principal before it can form an end-to-end promotion
    gate.
    """

    def __init__(self, store: Any):
        if not hasattr(store, "connection") or not hasattr(store, "_transaction"):
            raise TypeError("CapabilityRegistry requires a DaltonStore")
        self.store = store
        self.connection: sqlite3.Connection = store.connection
        self.connection.executescript(_SCHEMA_PATH.read_text(encoding="utf-8"))

    @property
    def conn(self) -> sqlite3.Connection:
        return self.connection

    @staticmethod
    def _row(row: sqlite3.Row | None) -> dict[str, Any] | None:
        return dict(row) if row is not None else None

    def _proposal(self, proposal: Any) -> tuple[CapabilityProposal, dict[str, Any]]:
        data = _mapping(proposal, "proposal")
        try:
            parsed = CapabilityProposal.from_dict(data)
        except Exception as exc:
            raise CapabilityRegistryError(str(exc)) from exc
        return parsed, parsed.to_dict()

    def _revision(self, cur: sqlite3.Cursor, reference: str) -> sqlite3.Row:
        row = cur.execute(
            "SELECT * FROM capability_proposal_versions WHERE revision_id=?", (reference,)
        ).fetchone()
        if row is None:
            row = cur.execute(
                "SELECT * FROM capability_proposal_versions WHERE capability_ref=? "
                "ORDER BY version_number DESC LIMIT 1", (reference,)
            ).fetchone()
        if row is None:
            raise CapabilityNotFound(f"capability proposal not found: {reference}")
        return row

    @staticmethod
    def _invocation_id(value: Any) -> str | None:
        if value is None:
            return None
        if isinstance(value, str):
            return value
        raise CapabilityRegistryError(
            "inline invocation payloads are forbidden; register the invocation with Core first"
        )

    def _invocation_row(self, cur: sqlite3.Cursor, value: Any, *, required: bool = True) -> sqlite3.Row | None:
        invocation_id = self._invocation_id(value)
        if not invocation_id:
            if required:
                raise CapabilityRegistryError("invocation reference is required")
            return None
        if not isinstance(value, str):
            raise CapabilityRegistryError(
                "inline invocation payloads are forbidden; use a registered invocation ID"
            )
        row = cur.execute(
            "SELECT * FROM model_invocations WHERE invocation_id=?", (invocation_id,)
        ).fetchone()
        if row is None and required:
            raise CapabilityRegistryError(f"invocation not found: {invocation_id}")
        return row

    @staticmethod
    def _invocation_wire(row: sqlite3.Row) -> dict[str, Any]:
        result = dict(row)
        try:
            result.update(json.loads(result.get("invocation_json", "{}")))
        except (TypeError, json.JSONDecodeError):
            pass
        result["invocation_id"] = row["invocation_id"]
        return result

    def _active_policy(self, cur: sqlite3.Cursor) -> Mapping[str, Any]:
        row = cur.execute(
            "SELECT v.policy_version_id, v.policy_json, v.content_hash, v.effective_from, v.effective_until FROM governance_policy_pointer p "
            "JOIN governance_policy_versions v ON v.policy_version_id=p.policy_version_id "
            "WHERE p.pointer_id=1"
        ).fetchone()
        if row is None:
            raise PromotionRejected("no active governance policy")
        now = datetime.now(timezone.utc)
        effective_from = _policy_time(row[3], "effective_from")
        effective_until = _policy_time(row[4], "effective_until") if row[4] is not None else None
        if now < effective_from or (effective_until is not None and now >= effective_until):
            raise PromotionRejected("active governance policy is outside its effective interval")
        return {
            "policy_version_id": row[0],
            "policy": json.loads(row[1]),
            "policy_content_hash": row[2],
        }

    def _idempotency(
        self, cur: sqlite3.Cursor, key: str | None, operation: str, request_hash: str
    ) -> dict[str, Any] | None:
        if key is None:
            return None
        _nonempty(key, "idempotency_key")
        row = cur.execute(
            "SELECT * FROM capability_idempotency_keys WHERE idempotency_key=?", (key,)
        ).fetchone()
        if row is None:
            return None
        if row["operation"] != operation or row["request_hash"] != request_hash:
            return {"status": "conflict", "idempotency_key": key, "request_hash": request_hash,
                    "existing_request_hash": row["request_hash"]}
        result = json.loads(row["result_json"])
        return {**result, "status": "duplicate", "idempotency_key": key}

    def _save_idempotency(self, cur: sqlite3.Cursor, key: str | None, operation: str, request_hash: str, result: Mapping[str, Any]) -> None:
        if key is None:
            return
        cur.execute(
            "INSERT INTO capability_idempotency_keys(idempotency_key,operation,request_hash,result_json,created_at) VALUES(?,?,?,?,?)",
            (key, operation, request_hash, canonical_json(result), _now()),
        )

    def submit_proposal(
        self,
        proposal: Any,
        *,
        capability_ref: str | None = None,
        version_number: int | None = None,
        artifact_hash: str | None = None,
        builder_invocation: Any = None,
        builder_invocation_ref: str | None = None,
        actor_ref: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        parsed, wire = self._proposal(proposal)
        if parsed.status != "proposed":
            raise CapabilityRegistryError("capability proposal status must be 'proposed' before registry submission")
        declared_fixtures = _validate_fixtures(wire.get("fixtures"), "proposal.fixtures")
        _validate_permission_document(wire.get("permissions"), "proposal.permissions")
        revision_id = _nonempty(wire["id"], "proposal.id")
        prior_ref = wire.get("prior_capability_ref")
        # ``prior_capability_ref`` is a revision reference, not the stable
        # capability key.  A first revision therefore owns its own key; later
        # revisions inherit the key from the prior row below.
        cap_ref = capability_ref or wire.get("capability_ref") or revision_id
        builder_value = builder_invocation if builder_invocation is not None else builder_invocation_ref
        if builder_value is None:
            participants = wire.get("participants", {})
            for key in _BUILDER_KEYS:
                if participants.get(key):
                    builder_value = participants[key]
                    break
        builder_id = self._invocation_id(builder_value)
        artifact_hash = artifact_hash or content_hash(wire.get("artifact_refs", []))
        _sha256(artifact_hash, "artifact_hash")
        actor = actor_ref or wire.get("participants", {}).get("actor_ref", "agent:dalton")
        _nonempty(actor, "actor_ref")
        request_hash = content_hash({"operation": "submit_proposal", "proposal": wire, "capability_ref": cap_ref,
                                     "version_number": version_number, "artifact_hash": artifact_hash,
                                     "builder_invocation_ref": builder_id, "actor_ref": actor})
        proposal_hash = content_hash(wire)
        with self.store._transaction() as cur:
            # Check an existing revision before resolving the new prior ref.
            # This makes every provenance mismatch an explicit collision,
            # including a changed/nonexistent prior reference.
            existing = cur.execute(
                "SELECT * FROM capability_proposal_versions WHERE revision_id=?", (revision_id,)
            ).fetchone()
            if existing:
                comparison_cap_ref = existing["capability_ref"] if prior_ref == existing["prior_revision_id"] else cap_ref
                same_revision = (
                    existing["capability_ref"] == comparison_cap_ref
                    and (version_number is None or existing["version_number"] == int(version_number))
                    and existing["prior_revision_id"] == prior_ref
                    and existing["builder_invocation_id"] == builder_id
                    and existing["actor_ref"] == actor
                    and existing["proposal_hash"] == proposal_hash
                    and existing["content_hash"] == content_hash({
                        "schema_version": _SCHEMA_VERSION, "id": revision_id,
                        "created_at": existing["created_at"], "capability_version_ref": revision_id,
                        "version": existing["version_number"], "proposal": wire,
                        "artifact_hash": artifact_hash, "prior_version_ref": prior_ref,
                        "builder_invocation_ref": builder_id, "actor_ref": actor,
                    })
                    and existing["artifact_hash"] == artifact_hash
                )
                if not same_revision:
                    raise CapabilityConflict(f"proposal revision provenance collision: {revision_id}")
                return {**self._proposal_result(existing, status="duplicate"),
                        **({"idempotency_key": idempotency_key} if idempotency_key else {})}
            prior_row = None
            if prior_ref:
                prior_row = self._revision(cur, str(prior_ref))
                inherited_cap_ref = prior_row["capability_ref"]
                if capability_ref is None and not wire.get("capability_ref"):
                    cap_ref = inherited_cap_ref
                if prior_row["capability_ref"] != cap_ref:
                    raise CapabilityConflict("prior proposal belongs to a different capability")
                inferred_version = int(prior_row["version_number"]) + 1
            else:
                inferred_version = 1
            version = inferred_version if version_number is None else int(version_number)
            if version < 1 or version != inferred_version:
                raise CapabilityConflict("proposal version must continue its prior revision chain")
            idem = self._idempotency(cur, idempotency_key, "submit_proposal", request_hash)
            if idem is not None:
                return idem
            if builder_value is not None:
                self._invocation_row(cur, builder_value, required=True)
            now = _now()
            version_base = {
                "schema_version": _SCHEMA_VERSION, "id": revision_id, "created_at": now,
                "capability_version_ref": revision_id, "version": version,
                "proposal": wire, "artifact_hash": artifact_hash,
                "prior_version_ref": prior_ref, "builder_invocation_ref": builder_id,
                "actor_ref": actor,
            }
            version_wire = dict(version_base, content_hash=content_hash(version_base))
            try:
                CapabilityVersion.from_dict(version_wire)
            except CapabilityRegistryError:
                raise
            cur.execute(
                "INSERT INTO capability_proposal_versions(revision_id,capability_ref,version_number,proposal_json,proposal_hash,content_hash,artifact_hash,prior_revision_id,builder_invocation_id,actor_ref,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (revision_id, cap_ref, version, canonical_json(wire), proposal_hash, version_wire["content_hash"], artifact_hash,
                 prior_ref, builder_id, actor, now),
            )
            result = {
                "status": "fresh", "revision_id": revision_id, "capability_ref": cap_ref,
                "version_number": version, "content_hash": version_wire["content_hash"],
                "proposal_hash": proposal_hash, "artifact_hash": artifact_hash,
                "prior_revision_id": prior_ref, "builder_invocation_id": builder_id,
            }
            self._save_idempotency(cur, idempotency_key, "submit_proposal", request_hash, result)
            return {**result, **({"idempotency_key": idempotency_key} if idempotency_key else {})}

    @staticmethod
    def _proposal_result(row: sqlite3.Row, *, status: str = "fresh") -> dict[str, Any]:
        return {"status": status, "revision_id": row["revision_id"], "capability_ref": row["capability_ref"],
                "version_number": row["version_number"], "content_hash": row["content_hash"],
                "proposal_hash": row["proposal_hash"],
                "artifact_hash": row["artifact_hash"], "prior_revision_id": row["prior_revision_id"],
                "builder_invocation_id": row["builder_invocation_id"]}

    register_proposal = submit_proposal
    propose = submit_proposal

    def record_evaluation(
        self,
        proposal_ref: str,
        *,
        evaluation_id: str | None = None,
        fixtures: Sequence[Any] = (),
        baseline: Mapping[str, Any] | None = None,
        results: Mapping[str, Any] | None = None,
        environment_hash: str,
        evaluator_invocation: Any,
        proposal_hash: str | None = None,
        actor_ref: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        try:
            fixtures = _validate_fixtures(fixtures, "evaluation.fixtures")
        except CapabilityRegistryError as exc:
            raise EvaluationRejected(str(exc)) from exc
        baseline_doc = _json_value(baseline or {}, "baseline")
        results_doc = _json_value(results or {}, "results")
        if not isinstance(baseline_doc, Mapping) or not isinstance(results_doc, Mapping):
            raise EvaluationRejected("baseline and results must be objects")
        try:
            env_hash = _sha256(environment_hash, "environment_hash")
            if proposal_hash is not None:
                _sha256(proposal_hash, "proposal_hash")
        except CapabilityRegistryError as exc:
            raise EvaluationRejected(str(exc)) from exc
        evaluator_id = self._invocation_id(evaluator_invocation)
        if not evaluator_id:
            raise EvaluationRejected("evaluator invocation is required")
        if idempotency_key is not None and evaluation_id is None:
            raise EvaluationRejected("evaluation_id is required when idempotency_key is supplied")
        eid = _id(evaluation_id)
        request_hash = content_hash({"operation": "record_evaluation", "proposal_ref": proposal_ref,
                                     "evaluation_id": eid, "fixtures": list(fixtures), "baseline": baseline_doc,
                                     "results": results_doc, "environment_hash": env_hash,
                                     "evaluator_invocation_ref": evaluator_id, "proposal_hash": proposal_hash})
        with self.store._transaction() as cur:
            idem = self._idempotency(cur, idempotency_key, "record_evaluation", request_hash)
            if idem is not None:
                return idem
            revision = self._revision(cur, str(proposal_ref))
            if proposal_hash is not None and proposal_hash != revision["proposal_hash"]:
                raise EvaluationRejected("proposal hash does not match immutable revision")
            declared_fixtures = _validate_fixtures(
                json.loads(revision["proposal_json"]).get("fixtures"),
                "proposal.fixtures",
            )
            if set(fixtures) != set(declared_fixtures):
                raise EvaluationRejected("evaluation fixtures must exactly match the immutable proposal declaration")
            builder_id = revision["builder_invocation_id"]
            if not builder_id:
                raise EvaluationRejected("proposal has no builder invocation provenance")
            builder = self._invocation_row(cur, builder_id, required=True)
            evaluator = self._invocation_row(cur, evaluator_invocation, required=True)
            if builder["invocation_id"] == evaluator["invocation_id"]:
                raise EvaluationRejected("builder invocation cannot evaluate its own capability")
            active_policy = self._active_policy(cur)
            independent, reason = check_independence(active_policy["policy"], self._invocation_wire(builder), self._invocation_wire(evaluator))
            if not independent:
                raise EvaluationRejected(reason or "evaluation invocation is not independent")
            existing = cur.execute("SELECT * FROM capability_evaluations WHERE evaluation_id=?", (eid,)).fetchone()
            if existing:
                same_evaluation = (
                    existing["revision_id"] == revision["revision_id"]
                    and existing["proposal_content_hash"] == revision["proposal_hash"]
                    and existing["fixtures_json"] == canonical_json(list(fixtures))
                    and existing["baseline_json"] == canonical_json(baseline_doc)
                    and existing["results_json"] == canonical_json(results_doc)
                    and existing["environment_hash"] == env_hash
                    and existing["evaluator_invocation_id"] == evaluator["invocation_id"]
                    and existing["policy_version_id"] == active_policy["policy_version_id"]
                    and existing["policy_content_hash"] == active_policy["policy_content_hash"]
                )
                if not same_evaluation:
                    raise CapabilityConflict(f"evaluation collision: {eid}")
                result = {"status": "duplicate", "evaluation_id": eid, "revision_id": revision["revision_id"], "content_hash": existing["content_hash"]}
                return {**result, **({"idempotency_key": idempotency_key} if idempotency_key else {})}
            actor = actor_ref or self._invocation_wire(evaluator).get("actor_ref", "agent:dalton")
            base = {"schema_version": _SCHEMA_VERSION, "id": eid, "created_at": _now(),
                    "capability_version_ref": revision["revision_id"], "proposal_content_hash": revision["proposal_hash"],
                    "fixtures": list(fixtures), "baseline": baseline_doc, "results": results_doc,
                    "environment_hash": env_hash, "evaluator_invocation_ref": evaluator["invocation_id"],
                    "builder_invocation_ref": builder["invocation_id"],
                    "policy_version_id": active_policy["policy_version_id"],
                    "policy_content_hash": active_policy["policy_content_hash"], "actor_ref": actor}
            evaluation_wire = dict(base, content_hash=content_hash(base))
            CapabilityEvaluation.from_dict(evaluation_wire)
            eval_hash = evaluation_wire["content_hash"]
            cur.execute(
                "INSERT INTO capability_evaluations(evaluation_id,revision_id,capability_ref,proposal_content_hash,fixtures_json,baseline_json,results_json,environment_hash,evaluator_invocation_id,builder_invocation_id,policy_version_id,policy_content_hash,content_hash,actor_ref,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (eid, revision["revision_id"], revision["capability_ref"], revision["proposal_hash"], canonical_json(list(fixtures)), canonical_json(baseline_doc), canonical_json(results_doc), env_hash, evaluator["invocation_id"], builder["invocation_id"], active_policy["policy_version_id"], active_policy["policy_content_hash"], eval_hash, actor, base["created_at"]),
            )
            result = {"status": "fresh", "evaluation_id": eid, "revision_id": revision["revision_id"],
                      "capability_ref": revision["capability_ref"], "content_hash": eval_hash,
                      "evaluator_invocation_id": evaluator["invocation_id"], "builder_invocation_id": builder["invocation_id"]}
            self._save_idempotency(cur, idempotency_key, "record_evaluation", request_hash, result)
            return {**result, **({"idempotency_key": idempotency_key} if idempotency_key else {})}

    evaluate = record_evaluation
    add_evaluation = record_evaluation

    def decide_promotion(
        self,
        proposal_ref: str,
        *,
        decision: str,
        actor_ref: str,
        evaluation_id: str | None = None,
        decision_id: str | None = None,
        requested_permissions: Mapping[str, Any] | None = None,
        rationale: str = "",
        rollback_to_revision_ref: str | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        if decision not in _DECISIONS:
            raise PromotionRejected(f"invalid promotion decision: {decision!r}")
        actor = _nonempty(actor_ref, "actor_ref")
        if decision in {"approve", "rollback"} and not _is_human(actor):
            raise PromotionRejected("reusable capability promotion requires a human actor")
        did = _id(decision_id)
        with self.store._transaction() as cur:
            revision = self._revision(cur, str(proposal_ref))
            proposal_doc = json.loads(revision["proposal_json"])
            allowed = proposal_doc.get("permissions", {})
            # Omitted permissions mean the proposal's declared permissions,
            # not an ambiguous empty permission set.
            requested = _json_value(
                allowed if requested_permissions is None else requested_permissions,
                "requested_permissions",
            )
            _validate_permission_document(allowed, "proposal.permissions")
            _validate_permission_document(requested, "requested_permissions")
            if not isinstance(requested, Mapping):
                raise PermissionEscalation("requested_permissions must be an object")
            if not _permission_subset(requested, allowed):
                raise PermissionEscalation("promotion permissions exceed proposal permissions")
            evaluation = None
            if evaluation_id is not None:
                evaluation = cur.execute("SELECT * FROM capability_evaluations WHERE evaluation_id=?", (evaluation_id,)).fetchone()
                if evaluation is None or evaluation["revision_id"] != revision["revision_id"]:
                    raise PromotionRejected("evaluation does not attest to this immutable proposal revision")
            if decision == "approve":
                if evaluation is None:
                    raise PromotionRejected("approval requires an evaluation")
                active_policy = self._active_policy(cur)
                if (evaluation["policy_version_id"] != active_policy["policy_version_id"]
                        or evaluation["policy_content_hash"] != active_policy["policy_content_hash"]):
                    raise PromotionRejected("evaluation was produced under a stale governance policy; re-evaluation is required")
                results = json.loads(evaluation["results_json"])
                status = str(results.get("status", "")).lower()
                if results.get("passed") is not True and status not in {"pass", "passed"}:
                    raise PromotionRejected("approval requires explicit successful evaluation evidence")
            rollback_row = None
            if decision == "rollback":
                current_pointer = cur.execute(
                    "SELECT * FROM capability_registry_pointers WHERE capability_ref=? "
                    "ORDER BY pointer_seq DESC LIMIT 1", (revision["capability_ref"],)
                ).fetchone()
                if current_pointer is None:
                    raise PromotionRejected("cannot rollback a capability with no active pointer")
                target = rollback_to_revision_ref
                if not target:
                    raise PromotionRejected("rollback_to_revision_ref is required")
                rollback_row = self._revision(cur, str(target))
                if rollback_row["capability_ref"] != revision["capability_ref"]:
                    raise PromotionRejected("rollback target belongs to a different capability")
                historical_active = cur.execute(
                    "SELECT 1 FROM capability_registry_pointers WHERE capability_ref=? "
                    "AND revision_id=? AND action='active' LIMIT 1",
                    (revision["capability_ref"], rollback_row["revision_id"]),
                ).fetchone()
                if historical_active is None:
                    raise PromotionRejected("rollback target was never active through an approval")
            request_hash = content_hash({"operation": "decide_promotion", "revision_id": revision["revision_id"],
                                         "evaluation_id": evaluation_id, "decision": decision, "actor_ref": actor,
                                         "requested_permissions": requested, "rationale": rationale,
                                         "rollback_to_revision_ref": rollback_to_revision_ref})
            idem = self._idempotency(cur, idempotency_key, "decide_promotion", request_hash)
            if idem is not None:
                return idem
            existing = cur.execute("SELECT * FROM capability_decisions WHERE decision_id=?", (did,)).fetchone()
            if existing:
                same_decision = (
                    existing["revision_id"] == revision["revision_id"]
                    and existing["evaluation_id"] == evaluation_id
                    and existing["decision"] == decision
                    and existing["actor_ref"] == actor
                    and existing["requested_permissions_json"] == canonical_json(requested)
                    and existing["rationale"] == rationale
                    and existing["rollback_to_revision_id"] == (rollback_row["revision_id"] if rollback_row else None)
                )
                if not same_decision:
                    raise CapabilityConflict(f"decision collision: {did}")
                return {"status": "duplicate", "decision_id": did, "capability_ref": revision["capability_ref"], "decision": decision}
            created = _now()
            base = {"schema_version": _SCHEMA_VERSION, "id": did, "created_at": created,
                    "capability_version_ref": revision["revision_id"], "evaluation_ref": evaluation_id,
                    "decision": decision, "actor_ref": actor, "requested_permissions": requested,
                    "rationale": rationale, "rollback_to_version_ref": rollback_row["revision_id"] if rollback_row else None}
            decision_wire = dict(base, content_hash=content_hash(base))
            CapabilityDecision.from_dict(decision_wire)
            decision_hash = decision_wire["content_hash"]
            cur.execute(
                "INSERT INTO capability_decisions(decision_id,revision_id,evaluation_id,capability_ref,decision,actor_ref,requested_permissions_json,rationale,rollback_to_revision_id,content_hash,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (did, revision["revision_id"], evaluation_id, revision["capability_ref"], decision, actor, canonical_json(requested), rationale, rollback_row["revision_id"] if rollback_row else None, decision_hash, created),
            )
            pointer_result = None
            if decision in {"approve", "rollback"}:
                target_revision = rollback_row["revision_id"] if rollback_row else revision["revision_id"]
                prior_pointer = cur.execute("SELECT pointer_seq FROM capability_registry_pointers WHERE capability_ref=? ORDER BY pointer_seq DESC LIMIT 1", (revision["capability_ref"],)).fetchone()
                cur.execute(
                    "INSERT INTO capability_registry_pointers(capability_ref,revision_id,action,decision_id,prior_pointer_seq,created_at) VALUES(?,?,?,?,?,?)",
                    (revision["capability_ref"], target_revision, "rollback" if decision == "rollback" else "active", did, prior_pointer[0] if prior_pointer else None, created),
                )
                pointer_result = cur.lastrowid
            result = {"status": "fresh", "decision_id": did, "capability_ref": revision["capability_ref"],
                      "revision_id": revision["revision_id"], "decision": decision, "pointer_seq": pointer_result,
                      "active_revision_id": (rollback_row["revision_id"] if rollback_row else revision["revision_id"]) if decision in {"approve", "rollback"} else None,
                      "content_hash": decision_hash}
            self._save_idempotency(cur, idempotency_key, "decide_promotion", request_hash, result)
            return {**result, **({"idempotency_key": idempotency_key} if idempotency_key else {})}

    promote = decide_promotion

    def rollback(self, capability_ref: str, target_revision_ref: str, *, actor_ref: str, reason: str = "rollback", decision_id: str | None = None, idempotency_key: str | None = None) -> dict[str, Any]:
        return self.decide_promotion(capability_ref, decision="rollback", actor_ref=actor_ref,
                                     rollback_to_revision_ref=target_revision_ref, rationale=reason,
                                     decision_id=decision_id, idempotency_key=idempotency_key)

    def active_pointer(self, capability_ref: str) -> dict[str, Any] | None:
        row = self.connection.execute(
            "SELECT * FROM capability_registry_pointers WHERE capability_ref=? ORDER BY pointer_seq DESC LIMIT 1", (capability_ref,)
        ).fetchone()
        if row is None:
            return None
        return dict(row)

    current_pointer = active_pointer

    def pointer_history(self, capability_ref: str) -> list[dict[str, Any]]:
        rows = self.connection.execute(
            "SELECT * FROM capability_registry_pointers WHERE capability_ref=? ORDER BY pointer_seq", (capability_ref,)
        ).fetchall()
        return [dict(row) for row in rows]

    def get_proposal(self, revision_ref: str) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT * FROM capability_proposal_versions WHERE revision_id=?", (revision_ref,)
        ).fetchone()
        if row is None:
            raise CapabilityNotFound(revision_ref)
        result = self._proposal_result(row)
        result["proposal"] = json.loads(row["proposal_json"])
        return result

    def get_version(self, revision_ref: str) -> dict[str, Any]:
        """Return and validate the complete CapabilityVersion wire object."""
        row = self.connection.execute(
            "SELECT * FROM capability_proposal_versions WHERE revision_id=?", (revision_ref,)
        ).fetchone()
        if row is None:
            raise CapabilityNotFound(revision_ref)
        base = {
            "schema_version": _SCHEMA_VERSION, "id": row["revision_id"], "created_at": row["created_at"],
            "capability_version_ref": row["revision_id"], "version": row["version_number"],
            "proposal": json.loads(row["proposal_json"]), "artifact_hash": row["artifact_hash"],
            "prior_version_ref": row["prior_revision_id"], "builder_invocation_ref": row["builder_invocation_id"],
            "actor_ref": row["actor_ref"],
        }
        wire = dict(base, content_hash=row["content_hash"])
        return CapabilityVersion.from_dict(wire).to_dict()

    def get_evaluation(self, evaluation_id: str) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT * FROM capability_evaluations WHERE evaluation_id=?", (evaluation_id,)
        ).fetchone()
        if row is None:
            raise CapabilityNotFound(evaluation_id)
        base = {
            "schema_version": _SCHEMA_VERSION, "id": row["evaluation_id"], "created_at": row["created_at"],
            "capability_version_ref": row["revision_id"], "proposal_content_hash": row["proposal_content_hash"],
            "fixtures": json.loads(row["fixtures_json"]), "baseline": json.loads(row["baseline_json"]),
            "results": json.loads(row["results_json"]), "environment_hash": row["environment_hash"],
            "evaluator_invocation_ref": row["evaluator_invocation_id"], "builder_invocation_ref": row["builder_invocation_id"],
            "policy_version_id": row["policy_version_id"], "policy_content_hash": row["policy_content_hash"],
            "actor_ref": row["actor_ref"],
        }
        return CapabilityEvaluation.from_dict(dict(base, content_hash=row["content_hash"])).to_dict()

    def get_decision(self, decision_id: str) -> dict[str, Any]:
        row = self.connection.execute(
            "SELECT * FROM capability_decisions WHERE decision_id=?", (decision_id,)
        ).fetchone()
        if row is None:
            raise CapabilityNotFound(decision_id)
        base = {
            "schema_version": _SCHEMA_VERSION, "id": row["decision_id"], "created_at": row["created_at"],
            "capability_version_ref": row["revision_id"], "evaluation_ref": row["evaluation_id"],
            "decision": row["decision"], "actor_ref": row["actor_ref"],
            "requested_permissions": json.loads(row["requested_permissions_json"]),
            "rationale": row["rationale"], "rollback_to_version_ref": row["rollback_to_revision_id"],
        }
        return CapabilityDecision.from_dict(dict(base, content_hash=row["content_hash"])).to_dict()


Registry = CapabilityRegistry
