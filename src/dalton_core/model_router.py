"""Versioned, deterministic model routing for Dalton Core.

The router stores logical credential-slot references, never credential values.
Every initial selection, retry, and switch produces a new immutable decision;
there is no in-process or provider-side silent fallback.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import sqlite3
import uuid
from collections.abc import Callable, Mapping, Sequence
from contextlib import contextmanager
from datetime import datetime, timezone
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any, Iterator

from .contracts import WorkOrder


SCHEMA_VERSION = "0.1"
_SCHEMA_PATH = Path(__file__).with_name("model_router_schema.sql")
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_REF_RE = re.compile(r"^[A-Za-z][A-Za-z0-9+._-]*:[^\s]+$")
_SLOT_RE = re.compile(r"^credential[-_]slot:[A-Za-z0-9][A-Za-z0-9._:/-]*$")
_TOKEN_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/+-]*$")
_DECISION_KINDS = frozenset({"initial", "retry", "switch"})
_AVAILABILITY_STATES = frozenset({"available", "degraded", "unavailable"})
_PREFERENCE_FIELDS = frozenset(
    {
        "estimated_cost_usd",
        "context.max_context_tokens",
        "context.max_output_tokens",
        "limits.max_total_tokens",
        "cost.input_per_million_usd",
        "cost.output_per_million_usd",
        "provider",
        "model",
        "family",
        "profile_id",
        "profile_version_ref",
    }
)
_BUDGET_FIELDS = (
    "max_input_tokens",
    "max_output_tokens",
    "max_total_tokens",
    "max_cost_usd",
)


class ModelRouterError(Exception):
    """Base class for model-router failures."""


class ModelRouterValidationError(ModelRouterError, ValueError):
    pass


class ModelRouterConflict(ModelRouterError):
    pass


class ModelProfileNotFound(ModelRouterError):
    pass


class RoutingPolicyNotFound(ModelRouterError):
    pass


class RouteDecisionNotFound(ModelRouterError):
    pass


class RouteTransitionError(ModelRouterError):
    pass


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def canonical_hash(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _timestamp(value: datetime) -> str:
    if not isinstance(value, datetime) or value.tzinfo is None:
        raise ModelRouterValidationError(
            "model router clock must return a timezone-aware datetime"
        )
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


def _parse_time(value: Any, name: str) -> datetime:
    if not isinstance(value, str) or not value:
        raise ModelRouterValidationError(f"{name} must be an RFC3339 timestamp")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ModelRouterValidationError(f"{name} must be an RFC3339 timestamp") from exc
    if parsed.tzinfo is None:
        raise ModelRouterValidationError(f"{name} must include a timezone")
    return parsed.astimezone(timezone.utc)


def _closed(
    value: Any,
    *,
    allowed: set[str],
    required: set[str],
    name: str,
) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise ModelRouterValidationError(f"{name} must be an object")
    unknown = set(value) - allowed
    missing = required - set(value)
    if unknown:
        raise ModelRouterValidationError(f"{name} unknown field(s): {sorted(unknown)}")
    if missing:
        raise ModelRouterValidationError(f"{name} missing field(s): {sorted(missing)}")
    return value


def _string(value: Any, name: str) -> str:
    if not isinstance(value, str) or not value:
        raise ModelRouterValidationError(f"{name} must be a non-empty string")
    return value


def _token(value: Any, name: str) -> str:
    result = _string(value, name)
    if not _TOKEN_RE.fullmatch(result):
        raise ModelRouterValidationError(f"{name} must be a canonical token")
    return result


def _ref(value: Any, name: str, *, nullable: bool = False) -> str | None:
    if value is None and nullable:
        return None
    result = _string(value, name)
    if not _REF_RE.fullmatch(result):
        raise ModelRouterValidationError(f"{name} must be an opaque namespaced reference")
    return result


def _slot(value: Any, name: str = "credential_slot_ref") -> str:
    result = _string(value, name)
    if not _SLOT_RE.fullmatch(result):
        raise ModelRouterValidationError(
            f"{name} must be a logical credential-slot reference, not a credential"
        )
    return result


def _positive_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ModelRouterValidationError(f"{name} must be a positive integer")
    return value


def _nonnegative_int(value: Any, name: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ModelRouterValidationError(f"{name} must be a non-negative integer")
    return value


def _decimal(value: Any, name: str, *, positive: bool = False) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ModelRouterValidationError(f"{name} must be a finite number")
    if isinstance(value, float) and not math.isfinite(value):
        raise ModelRouterValidationError(f"{name} must be a finite number")
    try:
        result = Decimal(str(value))
    except InvalidOperation as exc:
        raise ModelRouterValidationError(f"{name} must be a finite number") from exc
    if not result.is_finite() or result < 0 or (positive and result <= 0):
        qualifier = "positive" if positive else "non-negative"
        raise ModelRouterValidationError(f"{name} must be finite and {qualifier}")
    try:
        as_float = float(result)
    except (OverflowError, ValueError) as exc:
        raise ModelRouterValidationError(f"{name} is outside the supported range") from exc
    if not math.isfinite(as_float):
        raise ModelRouterValidationError(f"{name} is outside the supported range")
    return result


def _unique_tokens(
    value: Any, name: str, *, nonempty: bool = False
) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise ModelRouterValidationError(f"{name} must be an array")
    result = tuple(_token(item, f"{name}[]") for item in value)
    if nonempty and not result:
        raise ModelRouterValidationError(f"{name} must not be empty")
    if len(set(result)) != len(result):
        raise ModelRouterValidationError(f"{name} must contain unique values")
    return result


def _unique_refs(value: Any, name: str) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)):
        raise ModelRouterValidationError(f"{name} must be an array")
    result = tuple(_ref(item, f"{name}[]") for item in value)
    if len(set(result)) != len(result):
        raise ModelRouterValidationError(f"{name} must contain unique values")
    return result  # type: ignore[return-value]


def _money_string(value: Decimal) -> str:
    # Six decimal places are enough for routing estimates while remaining
    # canonical and directly comparable in audit fixtures.
    return format(value, ".6f")


def _profile_wire(data: Mapping[str, Any]) -> dict[str, Any]:
    keys = {
        "schema_version",
        "profile_version_ref",
        "id",
        "version",
        "created_at",
        "prior_version_ref",
        "provider",
        "model",
        "family",
        "adapter_ref",
        "credential_slot_ref",
        "capabilities",
        "modalities",
        "context",
        "availability",
        "cost",
        "limits",
        "content_hash",
    }
    required = keys - {"content_hash"}
    obj = _closed(data, allowed=keys, required=required, name="model endpoint profile")
    if obj["schema_version"] != SCHEMA_VERSION:
        raise ModelRouterValidationError("profile schema_version is unsupported")
    created_at = _string(obj["created_at"], "profile.created_at")
    _parse_time(created_at, "profile.created_at")
    context = _closed(
        obj["context"],
        allowed={"max_context_tokens", "max_output_tokens"},
        required={"max_context_tokens", "max_output_tokens"},
        name="profile.context",
    )
    availability = _closed(
        obj["availability"],
        allowed={"state", "checked_at", "valid_until"},
        required={"state", "checked_at", "valid_until"},
        name="profile.availability",
    )
    state = _string(availability["state"], "profile.availability.state")
    if state not in _AVAILABILITY_STATES:
        raise ModelRouterValidationError("profile availability state is invalid")
    checked_at = _string(availability["checked_at"], "profile.availability.checked_at")
    valid_until = _string(availability["valid_until"], "profile.availability.valid_until")
    checked_time = _parse_time(checked_at, "profile.availability.checked_at")
    valid_time = _parse_time(valid_until, "profile.availability.valid_until")
    if valid_time <= checked_time:
        raise ModelRouterValidationError(
            "profile availability valid_until must be after checked_at"
        )
    cost = _closed(
        obj["cost"],
        allowed={"currency", "input_per_million_usd", "output_per_million_usd"},
        required={"currency", "input_per_million_usd", "output_per_million_usd"},
        name="profile.cost",
    )
    if cost["currency"] != "USD":
        raise ModelRouterValidationError("profile.cost.currency must be USD")
    input_cost = _decimal(cost["input_per_million_usd"], "input_per_million_usd")
    output_cost = _decimal(cost["output_per_million_usd"], "output_per_million_usd")
    limits = _closed(
        obj["limits"],
        allowed=set(_BUDGET_FIELDS),
        required=set(_BUDGET_FIELDS),
        name="profile.limits",
    )
    max_context = _positive_int(context["max_context_tokens"], "max_context_tokens")
    max_output = _positive_int(context["max_output_tokens"], "max_output_tokens")
    limit_input = _positive_int(limits["max_input_tokens"], "limits.max_input_tokens")
    limit_output = _positive_int(limits["max_output_tokens"], "limits.max_output_tokens")
    limit_total = _positive_int(limits["max_total_tokens"], "limits.max_total_tokens")
    limit_cost = _decimal(limits["max_cost_usd"], "limits.max_cost_usd", positive=True)
    if limit_input > max_context or limit_output > max_output:
        raise ModelRouterValidationError("profile limits exceed declared model context")
    if limit_total > max_context + max_output:
        raise ModelRouterValidationError("profile total token limit exceeds model capacity")
    prior = _ref(obj["prior_version_ref"], "prior_version_ref", nullable=True)
    wire = {
        "schema_version": SCHEMA_VERSION,
        "profile_version_ref": _ref(obj["profile_version_ref"], "profile_version_ref"),
        "id": _ref(obj["id"], "profile.id"),
        "version": _positive_int(obj["version"], "profile.version"),
        "created_at": created_at,
        "prior_version_ref": prior,
        "provider": _token(obj["provider"], "profile.provider"),
        "model": _token(obj["model"], "profile.model"),
        "family": _token(obj["family"], "profile.family"),
        "adapter_ref": _ref(obj["adapter_ref"], "profile.adapter_ref"),
        "credential_slot_ref": _slot(obj["credential_slot_ref"]),
        "capabilities": list(
            _unique_tokens(obj["capabilities"], "profile.capabilities", nonempty=True)
        ),
        "modalities": list(
            _unique_tokens(obj["modalities"], "profile.modalities", nonempty=True)
        ),
        "context": {
            "max_context_tokens": max_context,
            "max_output_tokens": max_output,
        },
        "availability": {
            "state": state,
            "checked_at": checked_at,
            "valid_until": valid_until,
        },
        "cost": {
            "currency": "USD",
            "input_per_million_usd": float(input_cost),
            "output_per_million_usd": float(output_cost),
        },
        "limits": {
            "max_input_tokens": limit_input,
            "max_output_tokens": limit_output,
            "max_total_tokens": limit_total,
            "max_cost_usd": float(limit_cost),
        },
    }
    digest = canonical_hash(wire)
    asserted = obj.get("content_hash")
    if asserted is not None and asserted != digest:
        raise ModelRouterValidationError("profile content_hash mismatch")
    wire["content_hash"] = digest
    return wire


def _policy_wire(data: Mapping[str, Any]) -> dict[str, Any]:
    keys = {
        "schema_version",
        "policy_version_ref",
        "id",
        "version",
        "created_at",
        "prior_version_ref",
        "filters",
        "ordered_preferences",
        "content_hash",
    }
    required = keys - {"content_hash"}
    obj = _closed(data, allowed=keys, required=required, name="model routing policy")
    if obj["schema_version"] != SCHEMA_VERSION:
        raise ModelRouterValidationError("policy schema_version is unsupported")
    created_at = _string(obj["created_at"], "policy.created_at")
    _parse_time(created_at, "policy.created_at")
    filters = _closed(
        obj["filters"],
        allowed={
            "allowed_profile_ids",
            "allowed_providers",
            "allowed_families",
            "allowed_adapter_refs",
            "required_modalities",
            "family_independence_capabilities",
        },
        required={
            "allowed_profile_ids",
            "allowed_providers",
            "allowed_families",
            "allowed_adapter_refs",
            "required_modalities",
            "family_independence_capabilities",
        },
        name="policy.filters",
    )
    allowed_profile_ids = _unique_refs(filters["allowed_profile_ids"], "allowed_profile_ids")
    allowed_adapter_refs = _unique_refs(filters["allowed_adapter_refs"], "allowed_adapter_refs")
    preferences_raw = obj["ordered_preferences"]
    if not isinstance(preferences_raw, (list, tuple)) or not preferences_raw:
        raise ModelRouterValidationError("ordered_preferences must be a non-empty array")
    preferences: list[dict[str, str]] = []
    seen_fields: set[str] = set()
    for index, item in enumerate(preferences_raw):
        pref = _closed(
            item,
            allowed={"field", "direction"},
            required={"field", "direction"},
            name=f"ordered_preferences[{index}]",
        )
        field = _string(pref["field"], "preference.field")
        direction = _string(pref["direction"], "preference.direction")
        if field not in _PREFERENCE_FIELDS:
            raise ModelRouterValidationError(f"unsupported preference field: {field}")
        if direction not in {"asc", "desc"}:
            raise ModelRouterValidationError("preference direction must be asc or desc")
        if field in seen_fields:
            raise ModelRouterValidationError("preference fields must be unique")
        seen_fields.add(field)
        preferences.append({"field": field, "direction": direction})
    wire = {
        "schema_version": SCHEMA_VERSION,
        "policy_version_ref": _ref(obj["policy_version_ref"], "policy_version_ref"),
        "id": _ref(obj["id"], "policy.id"),
        "version": _positive_int(obj["version"], "policy.version"),
        "created_at": created_at,
        "prior_version_ref": _ref(
            obj["prior_version_ref"], "prior_version_ref", nullable=True
        ),
        "filters": {
            "allowed_profile_ids": list(allowed_profile_ids),
            "allowed_providers": list(
                _unique_tokens(filters["allowed_providers"], "allowed_providers")
            ),
            "allowed_families": list(
                _unique_tokens(filters["allowed_families"], "allowed_families")
            ),
            "allowed_adapter_refs": list(allowed_adapter_refs),
            "required_modalities": list(
                _unique_tokens(filters["required_modalities"], "required_modalities")
            ),
            "family_independence_capabilities": list(
                _unique_tokens(
                    filters["family_independence_capabilities"],
                    "family_independence_capabilities",
                )
            ),
        },
        "ordered_preferences": preferences,
    }
    digest = canonical_hash(wire)
    asserted = obj.get("content_hash")
    if asserted is not None and asserted != digest:
        raise ModelRouterValidationError("policy content_hash mismatch")
    wire["content_hash"] = digest
    return wire


class ModelRouter:
    """SQLite model catalog and deterministic selection service."""

    def __init__(
        self,
        path: str | Path = ":memory:",
        *,
        connection: sqlite3.Connection | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.path = str(path)
        self.clock = clock or _utc_now
        self._authorized = False
        self.connection = connection or sqlite3.connect(self.path, isolation_level=None)
        if connection is None and self.path != ":memory:":
            os.chmod(self.path, 0o600)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute("PRAGMA foreign_keys = ON")
        self.connection.execute("PRAGMA busy_timeout = 5000")
        self.connection.create_function(
            "dalton_model_router_authorized", 0, lambda: int(self._authorized)
        )
        self.connection.executescript(_SCHEMA_PATH.read_text(encoding="utf-8"))

    @property
    def conn(self) -> sqlite3.Connection:
        return self.connection

    def close(self) -> None:
        self.connection.close()

    def __enter__(self) -> "ModelRouter":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        self.close()

    def _now(self) -> datetime:
        value = self.clock()
        _timestamp(value)
        return value.astimezone(timezone.utc)

    @contextmanager
    def _transaction(self) -> Iterator[sqlite3.Cursor]:
        if self.connection.in_transaction:
            raise RuntimeError("ModelRouter operation cannot be nested")
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

    @staticmethod
    def _register_version(
        cur: sqlite3.Cursor,
        *,
        wire: Mapping[str, Any],
        table: str,
        version_ref_field: str,
        wire_entity_id_field: str,
        db_entity_id_field: str,
        hash_field: str,
        json_field: str,
        kind: str,
    ) -> dict[str, Any]:
        version_ref = wire[version_ref_field]
        entity_id = wire[wire_entity_id_field]
        digest = wire["content_hash"]
        existing = cur.execute(
            f"SELECT {hash_field}, {json_field} FROM {table} WHERE {version_ref_field}=?",
            (version_ref,),
        ).fetchone()
        if existing is not None:
            if existing[hash_field] == digest:
                return {"status": "duplicate", kind: json.loads(existing[json_field])}
            return {
                "status": "conflict",
                "reason": f"{version_ref_field} already has different content",
            }
        latest = cur.execute(
            f"SELECT {version_ref_field}, version FROM {table} "
            f"WHERE {db_entity_id_field}=? ORDER BY version DESC LIMIT 1",
            (entity_id,),
        ).fetchone()
        if latest is None:
            if wire["version"] != 1 or wire["prior_version_ref"] is not None:
                raise ModelRouterConflict(f"first {kind} version must be version 1 without prior")
        else:
            if wire["version"] != latest["version"] + 1:
                raise ModelRouterConflict(f"{kind} version must increase by exactly one")
            if wire["prior_version_ref"] != latest[version_ref_field]:
                raise ModelRouterConflict(f"{kind} prior_version_ref must reference latest version")
        return {"status": "fresh", kind: dict(wire)}

    def register_profile(self, profile: Mapping[str, Any]) -> dict[str, Any]:
        """Append one exact provider/model endpoint profile version."""
        wire = _profile_wire(profile)
        with self._transaction() as cur:
            result = self._register_version(
                cur,
                wire=wire,
                table="model_endpoint_profile_versions",
                version_ref_field="profile_version_ref",
                wire_entity_id_field="id",
                db_entity_id_field="profile_id",
                hash_field="profile_hash",
                json_field="profile_json",
                kind="profile",
            )
            if result["status"] != "fresh":
                return result
            cur.execute(
                "INSERT INTO model_endpoint_profile_versions "
                "(profile_version_ref, profile_id, version, prior_version_ref, provider, model, "
                "family, adapter_ref, credential_slot_ref, profile_hash, profile_json, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    wire["profile_version_ref"],
                    wire["id"],
                    wire["version"],
                    wire["prior_version_ref"],
                    wire["provider"],
                    wire["model"],
                    wire["family"],
                    wire["adapter_ref"],
                    wire["credential_slot_ref"],
                    wire["content_hash"],
                    canonical_json(wire),
                    wire["created_at"],
                ),
            )
            return result

    def register_policy(self, policy: Mapping[str, Any]) -> dict[str, Any]:
        """Append one routing-policy version with closed filters/preferences."""
        wire = _policy_wire(policy)
        with self._transaction() as cur:
            result = self._register_version(
                cur,
                wire=wire,
                table="model_routing_policy_versions",
                version_ref_field="policy_version_ref",
                wire_entity_id_field="id",
                db_entity_id_field="policy_id",
                hash_field="policy_hash",
                json_field="policy_json",
                kind="policy",
            )
            if result["status"] != "fresh":
                return result
            cur.execute(
                "INSERT INTO model_routing_policy_versions "
                "(policy_version_ref, policy_id, version, prior_version_ref, policy_hash, "
                "policy_json, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (
                    wire["policy_version_ref"],
                    wire["id"],
                    wire["version"],
                    wire["prior_version_ref"],
                    wire["content_hash"],
                    canonical_json(wire),
                    wire["created_at"],
                ),
            )
            return result

    def get_profile(self, profile_version_ref: str) -> dict[str, Any]:
        ref = _ref(profile_version_ref, "profile_version_ref")
        row = self.connection.execute(
            "SELECT profile_json FROM model_endpoint_profile_versions WHERE profile_version_ref=?",
            (ref,),
        ).fetchone()
        if row is None:
            raise ModelProfileNotFound(profile_version_ref)
        return json.loads(row["profile_json"])

    def get_policy(self, policy_version_ref: str) -> dict[str, Any]:
        ref = _ref(policy_version_ref, "policy_version_ref")
        row = self.connection.execute(
            "SELECT policy_json FROM model_routing_policy_versions WHERE policy_version_ref=?",
            (ref,),
        ).fetchone()
        if row is None:
            raise RoutingPolicyNotFound(policy_version_ref)
        return json.loads(row["policy_json"])

    def get_decision(self, decision_ref: str) -> dict[str, Any]:
        ref = _ref(decision_ref, "decision_ref")
        row = self.connection.execute(
            "SELECT decision_json FROM model_route_decisions WHERE decision_id=?", (ref,)
        ).fetchone()
        if row is None:
            raise RouteDecisionNotFound(decision_ref)
        return json.loads(row["decision_json"])

    def list_decisions(
        self, *, work_order_id: str | None = None
    ) -> list[dict[str, Any]]:
        if work_order_id is None:
            rows = self.connection.execute(
                "SELECT decision_json FROM model_route_decisions ORDER BY decision_sequence"
            ).fetchall()
        else:
            work_order_id = _string(work_order_id, "work_order_id")
            rows = self.connection.execute(
                "SELECT decision_json FROM model_route_decisions WHERE work_order_id=? "
                "ORDER BY decision_sequence",
                (work_order_id,),
            ).fetchall()
        return [json.loads(row["decision_json"]) for row in rows]

    @staticmethod
    def _work_wire(work_order: WorkOrder | Mapping[str, Any]) -> dict[str, Any]:
        if isinstance(work_order, WorkOrder):
            return work_order.to_dict()
        if not isinstance(work_order, Mapping):
            raise ModelRouterValidationError("work_order must be WorkOrder or mapping")
        try:
            return WorkOrder.from_dict(work_order).to_dict()
        except Exception as exc:
            raise ModelRouterValidationError(str(exc)) from exc

    @staticmethod
    def _latest_profiles(cur: sqlite3.Cursor) -> list[dict[str, Any]]:
        rows = cur.execute(
            "SELECT p.profile_json FROM model_endpoint_profile_versions p "
            "WHERE NOT EXISTS (SELECT 1 FROM model_endpoint_profile_versions newer "
            "WHERE newer.profile_id=p.profile_id AND newer.version>p.version) "
            "ORDER BY p.profile_id, p.profile_version_ref"
        ).fetchall()
        return [json.loads(row["profile_json"]) for row in rows]

    @staticmethod
    def _budget_values(budget: Mapping[str, Any]) -> tuple[dict[str, Any], list[str]]:
        parsed: dict[str, Any] = {}
        reasons: list[str] = []
        for field in _BUDGET_FIELDS:
            if field not in budget:
                reasons.append(f"work_order_budget_missing:{field}")
        if reasons:
            return parsed, reasons
        try:
            parsed["max_input_tokens"] = _positive_int(
                budget["max_input_tokens"], "budget.max_input_tokens"
            )
            parsed["max_output_tokens"] = _positive_int(
                budget["max_output_tokens"], "budget.max_output_tokens"
            )
            parsed["max_total_tokens"] = _positive_int(
                budget["max_total_tokens"], "budget.max_total_tokens"
            )
            parsed["max_cost_usd"] = _decimal(
                budget["max_cost_usd"], "budget.max_cost_usd", positive=True
            )
        except ModelRouterValidationError as exc:
            return {}, [f"work_order_budget_invalid:{exc}"]
        return parsed, []

    @staticmethod
    def _estimate_cost(
        profile: Mapping[str, Any], input_tokens: int, output_tokens: int
    ) -> Decimal:
        cost = profile["cost"]
        return (
            Decimal(str(cost["input_per_million_usd"])) * input_tokens
            + Decimal(str(cost["output_per_million_usd"])) * output_tokens
        ) / Decimal(1_000_000)

    @staticmethod
    def _preference_value(candidate: Mapping[str, Any], field: str) -> Any:
        profile = candidate["profile"]
        if field == "estimated_cost_usd":
            return candidate["estimated_cost_decimal"]
        if field.startswith("context."):
            return profile["context"][field.split(".", 1)[1]]
        if field.startswith("limits."):
            return profile["limits"][field.split(".", 1)[1]]
        if field.startswith("cost."):
            return Decimal(str(profile["cost"][field.split(".", 1)[1]]))
        if field == "profile_id":
            return profile["id"]
        return profile[field]

    @classmethod
    def _sort_candidates(
        cls, candidates: list[dict[str, Any]], preferences: Sequence[Mapping[str, Any]]
    ) -> list[dict[str, Any]]:
        # The implicit final tie-break is always the immutable profile version
        # reference, independent of insertion or SQLite row order.
        ordered = sorted(candidates, key=lambda item: item["profile"]["profile_version_ref"])
        for preference in reversed(preferences):
            ordered.sort(
                key=lambda item, field=preference["field"]: cls._preference_value(
                    item, field
                ),
                reverse=preference["direction"] == "desc",
            )
        return ordered

    @staticmethod
    def _assert_transition(
        cur: sqlite3.Cursor,
        *,
        decision_kind: str,
        previous_decision_ref: str | None,
        work_order_id: str,
        attempt_number: int,
        capability: str,
    ) -> tuple[dict[str, Any] | None, set[str]]:
        latest_row = cur.execute(
            "SELECT decision_id, decision_json FROM model_route_decisions "
            "WHERE work_order_id=? AND capability=? ORDER BY decision_sequence DESC LIMIT 1",
            (work_order_id, capability),
        ).fetchone()
        latest = json.loads(latest_row["decision_json"]) if latest_row else None
        if decision_kind == "initial":
            # Scheduler leases can expire before routing, so its first routed
            # attempt need not be attempt 1.
            if previous_decision_ref is not None:
                raise RouteTransitionError("initial route cannot reference a previous decision")
            if latest is not None:
                raise RouteTransitionError(
                    "route already exists; retry or switch must reference the latest decision"
                )
            return None, set()
        if previous_decision_ref is None:
            raise RouteTransitionError(f"{decision_kind} requires previous_decision_ref")
        if latest is None or latest["id"] != previous_decision_ref:
            raise RouteTransitionError("previous_decision_ref must be the latest route decision")
        if latest["work_order_ref"] != work_order_id or latest["capability"] != capability:
            raise RouteTransitionError("previous route belongs to different work/capability")
        # The same lease-expiry gap can occur between route decisions.  Route
        # lineage must move forward, but it must not invent missing decisions.
        if decision_kind == "retry" and attempt_number <= latest["attempt_number"]:
            raise RouteTransitionError("retry must advance the attempt number")
        if decision_kind == "switch" and attempt_number != latest["attempt_number"]:
            raise RouteTransitionError("switch must remain in the same attempt")
        excluded: set[str] = set()
        if decision_kind == "switch":
            # Walk the same-attempt chain so an explicit switch cannot silently
            # cycle A -> B -> A.
            current = latest
            while current is not None and current["attempt_number"] == attempt_number:
                selected = current.get("selected_profile_version_ref")
                if selected:
                    excluded.add(selected)
                prior = current.get("previous_decision_ref")
                if not prior:
                    break
                row = cur.execute(
                    "SELECT decision_json FROM model_route_decisions WHERE decision_id=?",
                    (prior,),
                ).fetchone()
                current = json.loads(row["decision_json"]) if row else None
        return latest, excluded

    def route(
        self,
        work_order: WorkOrder | Mapping[str, Any],
        *,
        attempt_number: int,
        capability: str,
        policy_version_ref: str,
        credential_slot_refs: Sequence[str],
        required_modalities: Sequence[str],
        required_context_tokens: int,
        estimated_input_tokens: int,
        estimated_output_tokens: int,
        idempotency_key: str,
        decision_kind: str = "initial",
        previous_decision_ref: str | None = None,
        producer_family: str | None = None,
    ) -> dict[str, Any]:
        """Persist one deterministic route decision.

        A selected result and a fail-closed rejection are both immutable route
        decisions.  Repeating the same idempotency key returns ``duplicate``;
        reusing it with different request semantics returns ``conflict``.
        """
        wire = self._work_wire(work_order)
        attempt_number = _positive_int(attempt_number, "attempt_number")
        capability = _token(capability, "capability")
        if capability not in wire["requested_capabilities"]:
            raise ModelRouterValidationError(
                "capability must be declared by the WorkOrder"
            )
        policy_version_ref = _ref(
            policy_version_ref, "policy_version_ref"
        )  # type: ignore[assignment]
        idempotency_key = _string(idempotency_key, "idempotency_key")
        if decision_kind not in _DECISION_KINDS:
            raise ModelRouterValidationError("decision_kind is invalid")
        previous_decision_ref = _ref(
            previous_decision_ref, "previous_decision_ref", nullable=True
        )
        slots = tuple(_slot(value, "credential_slot_refs[]") for value in credential_slot_refs)
        if len(set(slots)) != len(slots):
            raise ModelRouterValidationError("credential_slot_refs must be unique")
        modalities = _unique_tokens(
            required_modalities, "required_modalities", nonempty=True
        )
        required_context_tokens = _positive_int(
            required_context_tokens, "required_context_tokens"
        )
        estimated_input_tokens = _positive_int(
            estimated_input_tokens, "estimated_input_tokens"
        )
        estimated_output_tokens = _positive_int(
            estimated_output_tokens, "estimated_output_tokens"
        )
        if required_context_tokens < estimated_input_tokens:
            raise ModelRouterValidationError(
                "required_context_tokens cannot be smaller than estimated_input_tokens"
            )
        if producer_family is not None:
            producer_family = _token(producer_family, "producer_family")
        request = {
            "work_order_hash": canonical_hash(wire),
            "work_order_ref": wire["id"],
            "attempt_number": attempt_number,
            "capability": capability,
            "policy_version_ref": policy_version_ref,
            "credential_slot_refs": list(slots),
            "required_modalities": list(modalities),
            "required_context_tokens": required_context_tokens,
            "estimated_input_tokens": estimated_input_tokens,
            "estimated_output_tokens": estimated_output_tokens,
            "decision_kind": decision_kind,
            "previous_decision_ref": previous_decision_ref,
            "producer_family": producer_family,
        }
        request_hash = canonical_hash(request)
        now_dt = self._now()
        now = _timestamp(now_dt)
        with self._transaction() as cur:
            prior_idempotency = cur.execute(
                "SELECT request_hash, result_json FROM model_route_idempotency "
                "WHERE idempotency_key=?",
                (idempotency_key,),
            ).fetchone()
            if prior_idempotency is not None:
                if prior_idempotency["request_hash"] == request_hash:
                    result = json.loads(prior_idempotency["result_json"])
                    result["status"] = "duplicate"
                    return result
                return {
                    "status": "conflict",
                    "reason": "idempotency key already belongs to another route request",
                }
            policy_row = cur.execute(
                "SELECT policy_json FROM model_routing_policy_versions "
                "WHERE policy_version_ref=?",
                (policy_version_ref,),
            ).fetchone()
            if policy_row is None:
                raise RoutingPolicyNotFound(str(policy_version_ref))
            policy = json.loads(policy_row["policy_json"])
            _previous, switch_exclusions = self._assert_transition(
                cur,
                decision_kind=decision_kind,
                previous_decision_ref=previous_decision_ref,
                work_order_id=wire["id"],
                attempt_number=attempt_number,
                capability=capability,
            )
            budget, global_reasons = self._budget_values(wire["budget"])
            if budget:
                if estimated_input_tokens > budget["max_input_tokens"]:
                    global_reasons.append("work_order_budget_input_exceeded")
                if estimated_output_tokens > budget["max_output_tokens"]:
                    global_reasons.append("work_order_budget_output_exceeded")
                if (
                    estimated_input_tokens + estimated_output_tokens
                    > budget["max_total_tokens"]
                ):
                    global_reasons.append("work_order_budget_total_exceeded")
            filters = policy["filters"]
            if (
                capability in filters["family_independence_capabilities"]
                and producer_family is None
            ):
                global_reasons.append("producer_family_required")
            required_modality_set = set(modalities) | set(filters["required_modalities"])
            supplied_slots = set(slots)
            candidates: list[dict[str, Any]] = []
            snapshot: list[dict[str, Any]] = []
            for profile in self._latest_profiles(cur):
                reasons = list(global_reasons)
                if profile["profile_version_ref"] in switch_exclusions:
                    reasons.append("already_tried_in_switch_chain")
                if capability not in profile["capabilities"]:
                    reasons.append("capability_not_supported")
                if not required_modality_set.issubset(set(profile["modalities"])):
                    reasons.append("modality_not_supported")
                if (
                    filters["allowed_profile_ids"]
                    and profile["id"] not in filters["allowed_profile_ids"]
                ):
                    reasons.append("profile_not_allowed")
                if (
                    filters["allowed_providers"]
                    and profile["provider"] not in filters["allowed_providers"]
                ):
                    reasons.append("provider_not_allowed")
                if (
                    filters["allowed_families"]
                    and profile["family"] not in filters["allowed_families"]
                ):
                    reasons.append("family_not_allowed")
                if (
                    filters["allowed_adapter_refs"]
                    and profile["adapter_ref"] not in filters["allowed_adapter_refs"]
                ):
                    reasons.append("adapter_not_allowed")
                availability = profile["availability"]
                if availability["state"] != "available":
                    reasons.append("profile_not_available")
                if _parse_time(availability["checked_at"], "checked_at") > now_dt:
                    reasons.append("availability_check_in_future")
                if _parse_time(availability["valid_until"], "valid_until") <= now_dt:
                    reasons.append("availability_expired")
                if profile["credential_slot_ref"] not in supplied_slots:
                    reasons.append("credential_slot_unavailable")
                if required_context_tokens > profile["context"]["max_context_tokens"]:
                    reasons.append("context_window_insufficient")
                if estimated_output_tokens > profile["context"]["max_output_tokens"]:
                    reasons.append("model_output_limit_insufficient")
                limits = profile["limits"]
                if estimated_input_tokens > limits["max_input_tokens"]:
                    reasons.append("profile_input_limit_exceeded")
                if estimated_output_tokens > limits["max_output_tokens"]:
                    reasons.append("profile_output_limit_exceeded")
                if estimated_input_tokens + estimated_output_tokens > limits["max_total_tokens"]:
                    reasons.append("profile_total_limit_exceeded")
                estimate = self._estimate_cost(
                    profile, estimated_input_tokens, estimated_output_tokens
                )
                if estimate > Decimal(str(limits["max_cost_usd"])):
                    reasons.append("profile_cost_limit_exceeded")
                if budget and estimate > budget["max_cost_usd"]:
                    reasons.append("work_order_cost_budget_exceeded")
                if producer_family is not None and profile["family"] == producer_family:
                    reasons.append("model_family_not_independent")
                reasons = sorted(set(reasons))
                item = {
                    "profile_version_ref": profile["profile_version_ref"],
                    "profile_hash": profile["content_hash"],
                    "eligible": not reasons,
                    "rejection_reasons": reasons,
                    "estimated_cost_usd": _money_string(estimate),
                }
                snapshot.append(item)
                if not reasons:
                    candidates.append(
                        {
                            "profile": profile,
                            "estimated_cost_decimal": estimate,
                            "snapshot": item,
                        }
                    )
            snapshot.sort(key=lambda item: item["profile_version_ref"])
            snapshot_hash = canonical_hash(snapshot)
            selected = None
            if candidates:
                selected = self._sort_candidates(
                    candidates, policy["ordered_preferences"]
                )[0]["profile"]
            outcome = "selected" if selected is not None else "rejected"
            rejected_reasons = sorted(
                set(global_reasons)
                | (
                    {reason for item in snapshot for reason in item["rejection_reasons"]}
                    if selected is None
                    else set()
                )
            )
            if not snapshot:
                rejected_reasons.append("model_directory_empty")
            decision_id = f"route-decision:{uuid.uuid4().hex}"
            decision = {
                "schema_version": SCHEMA_VERSION,
                "id": decision_id,
                "created_at": now,
                "decision_kind": decision_kind,
                "outcome": outcome,
                "work_order_ref": wire["id"],
                "work_order_hash": request["work_order_hash"],
                "attempt_number": attempt_number,
                "capability": capability,
                "policy_version_ref": policy_version_ref,
                "policy_hash": policy["content_hash"],
                "candidate_snapshot_hash": snapshot_hash,
                "candidate_snapshot": snapshot,
                "constraints": {
                    "credential_slot_refs": list(slots),
                    "required_modalities": list(modalities),
                    "required_context_tokens": required_context_tokens,
                    "estimated_input_tokens": estimated_input_tokens,
                    "estimated_output_tokens": estimated_output_tokens,
                    "producer_family": producer_family,
                },
                "selected_profile_version_ref": (
                    selected["profile_version_ref"] if selected else None
                ),
                "selected_profile_hash": selected["content_hash"] if selected else None,
                "selected_endpoint": (
                    {
                        "provider": selected["provider"],
                        "model": selected["model"],
                        "family": selected["family"],
                        "adapter_ref": selected["adapter_ref"],
                        "credential_slot_ref": selected["credential_slot_ref"],
                    }
                    if selected
                    else None
                ),
                "previous_decision_ref": previous_decision_ref,
                "rejection_reasons": rejected_reasons,
                "request_hash": request_hash,
            }
            decision["content_hash"] = canonical_hash(decision)
            cur.execute(
                "INSERT INTO model_route_decisions "
                "(decision_id, decision_kind, outcome, work_order_id, work_order_hash, "
                "attempt_number, capability, policy_version_ref, policy_hash, "
                "candidate_snapshot_hash, selected_profile_version_ref, previous_decision_ref, "
                "request_hash, decision_hash, decision_json, created_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    decision_id,
                    decision_kind,
                    outcome,
                    wire["id"],
                    request["work_order_hash"],
                    attempt_number,
                    capability,
                    policy_version_ref,
                    policy["content_hash"],
                    snapshot_hash,
                    decision["selected_profile_version_ref"],
                    previous_decision_ref,
                    request_hash,
                    decision["content_hash"],
                    canonical_json(decision),
                    now,
                ),
            )
            result = {"status": "fresh", "decision": decision}
            cur.execute(
                "INSERT INTO model_route_idempotency "
                "(idempotency_key, request_hash, decision_id, result_json, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    idempotency_key,
                    request_hash,
                    decision_id,
                    canonical_json(result),
                    now,
                ),
            )
            return result

    def switch(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        kwargs["decision_kind"] = "switch"
        return self.route(*args, **kwargs)

    def retry(self, *args: Any, **kwargs: Any) -> dict[str, Any]:
        kwargs["decision_kind"] = "retry"
        return self.route(*args, **kwargs)


DeterministicModelRouter = ModelRouter
