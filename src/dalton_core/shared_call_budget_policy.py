"""Read the host-owned model call and run budget policy shared by workspaces."""
from __future__ import annotations
import json
import math
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from .store import content_hash

SCHEMA_VERSION = "dalton-shared-call-budget-policy-0.1"
LIMITS_SCHEMA_VERSION = "dalton-shared-call-budget-policy-0.2"
MEMO_MAINTENANCE_SCHEMA_VERSION = "dalton-shared-call-budget-policy-0.3"
LIMITS_SCHEMA_VERSIONS = {LIMITS_SCHEMA_VERSION, MEMO_MAINTENANCE_SCHEMA_VERSION}
_PURPOSE = re.compile(r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$")
_FIELDS = {"schema_version", "default_max_cost_usd", "purpose_max_cost_usd",
           "revision", "prior_hash", "updated_at", "actor_ref", "content_hash"}

class SharedCallBudgetPolicyError(ValueError): pass

def _cost(value: Any, label: str) -> float:
    if (isinstance(value, bool) or not isinstance(value, (int, float))
            or not math.isfinite(float(value)) or float(value) <= 0):
        raise SharedCallBudgetPolicyError(f"{label} must be a positive finite number")
    return float(value)

def validate_shared_call_budget_policy(value: Mapping[str, Any]) -> dict[str, Any]:
    version = value.get("schema_version") if isinstance(value, Mapping) else None
    fields = (_FIELDS | {"purpose_call_limits", "purpose_run_limits"}
              if version in LIMITS_SCHEMA_VERSIONS else _FIELDS)
    if not isinstance(value, Mapping) or set(value) != fields:
        raise SharedCallBudgetPolicyError("shared call budget policy has an invalid closed shape")
    wire = dict(value); asserted = wire.pop("content_hash")
    if wire.get("schema_version") not in {SCHEMA_VERSION, *LIMITS_SCHEMA_VERSIONS} or asserted != content_hash(wire):
        raise SharedCallBudgetPolicyError("shared call budget policy identity or hash is invalid")
    if not isinstance(wire.get("revision"), int) or isinstance(wire["revision"], bool) or wire["revision"] < 1:
        raise SharedCallBudgetPolicyError("shared call budget policy revision is invalid")
    if wire["prior_hash"] is not None and not re.fullmatch(r"[0-9a-f]{64}", str(wire["prior_hash"])):
        raise SharedCallBudgetPolicyError("shared call budget policy prior_hash is invalid")
    if not isinstance(wire.get("updated_at"), str) or not wire["updated_at"]:
        raise SharedCallBudgetPolicyError("shared call budget policy updated_at is invalid")
    if not isinstance(wire.get("actor_ref"), str) or not wire["actor_ref"].startswith("human:"):
        raise SharedCallBudgetPolicyError("shared call budget policy actor_ref is invalid")
    # Validate without changing the hashed JSON representation (1 and 1.0
    # are both valid values but have different serialized hashes).
    _cost(wire["default_max_cost_usd"], "default_max_cost_usd")
    if not isinstance(wire.get("purpose_max_cost_usd"), Mapping):
        raise SharedCallBudgetPolicyError("purpose_max_cost_usd must be an object")
    checked = {}
    for purpose, cost in wire["purpose_max_cost_usd"].items():
        if not isinstance(purpose, str) or not _PURPOSE.fullmatch(purpose):
            raise SharedCallBudgetPolicyError("purpose_max_cost_usd has a noncanonical purpose")
        _cost(cost, f"purpose_max_cost_usd.{purpose}")
        checked[purpose] = cost
    if version in LIMITS_SCHEMA_VERSIONS:
        from .call_budget import validate_budget_overrides, validate_run_budget_overrides
        for name, validate in (("purpose_call_limits", validate_budget_overrides),
                               ("purpose_run_limits", validate_run_budget_overrides)):
            if not isinstance(wire[name], Mapping):
                raise SharedCallBudgetPolicyError(f"{name} must be an object")
            for purpose, limits in wire[name].items():
                if not isinstance(purpose, str) or not _PURPOSE.fullmatch(purpose):
                    raise SharedCallBudgetPolicyError(f"{name} has a noncanonical purpose")
                try:
                    result = validate(limits)
                    from .call_budget import MEMO_MAINTENANCE_ROUNDS_FIELD
                    if MEMO_MAINTENANCE_ROUNDS_FIELD in result and (
                            version != MEMO_MAINTENANCE_SCHEMA_VERSION
                            or name != "purpose_run_limits" or purpose != "investment_memo"):
                        raise ValueError("memo maintenance policy requires the explicit 0.3 investment_memo run contract")
                    if name == "purpose_call_limits" and "max_cost_usd" in result:
                        raise ValueError("call cost belongs in purpose_max_cost_usd")
                except ValueError as exc:
                    raise SharedCallBudgetPolicyError(f"{name}.{purpose}: {exc}") from exc
    return {**wire, "purpose_max_cost_usd": checked, "content_hash": asserted}

def load_shared_call_budget_policy(path: str | Path) -> dict[str, Any]:
    target = Path(path).expanduser()
    if not target.is_absolute() or target.is_symlink():
        raise SharedCallBudgetPolicyError("shared call budget policy path must be absolute and not a symlink")
    try: value = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SharedCallBudgetPolicyError("shared call budget policy is unavailable or invalid") from exc
    return validate_shared_call_budget_policy(value)

def effective_shared_max_cost(policy: Mapping[str, Any], purpose: str) -> float:
    checked = validate_shared_call_budget_policy(policy)
    return float(checked["purpose_max_cost_usd"].get(purpose, checked["default_max_cost_usd"]))


def effective_shared_limits(policy: Mapping[str, Any], purpose: str, *, kind: str = "call") -> dict[str, Any]:
    """Only explicitly governed fields replace the installed purpose bounds."""
    if kind not in {"call", "run"}:
        raise SharedCallBudgetPolicyError("budget kind must be call or run")
    checked = validate_shared_call_budget_policy(policy)
    limits = dict((checked.get(f"purpose_{kind}_limits") or {}).get(purpose, {}))
    if kind == "call":
        limits["max_cost_usd"] = effective_shared_max_cost(checked, purpose)
    return limits


def updated_shared_limits(policy: Mapping[str, Any], *, purpose: str, kind: str,
                          budget: Mapping[str, Any], actor_ref: str, updated_at: str) -> dict[str, Any]:
    """Build a merged successor; never erase other purposes or omitted fields."""
    from .call_budget import validate_budget_overrides, validate_run_budget_overrides
    current = validate_shared_call_budget_policy(policy)
    if not isinstance(purpose, str) or not _PURPOSE.fullmatch(purpose):
        raise SharedCallBudgetPolicyError("purpose must be a canonical token")
    if kind not in {"call", "run"}:
        raise SharedCallBudgetPolicyError("budget kind must be call or run")
    checked = (validate_budget_overrides if kind == "call" else validate_run_budget_overrides)(budget)
    if not checked:
        raise SharedCallBudgetPolicyError("shared budget edit must contain a field")
    body = {key: value for key, value in current.items() if key != "content_hash"}
    cost = dict(current["purpose_max_cost_usd"])
    if kind == "call" and "max_cost_usd" in checked:
        requested_cost = checked.pop("max_cost_usd")
        if effective_shared_max_cost(current, purpose) != requested_cost:
            cost[purpose] = requested_cost
    body["purpose_max_cost_usd"] = cost
    # Cost-only edits to old records preserve their original wire version.
    from .call_budget import MEMO_MAINTENANCE_ROUNDS_FIELD
    if MEMO_MAINTENANCE_ROUNDS_FIELD in checked and (kind != "run" or purpose != "investment_memo"):
        raise SharedCallBudgetPolicyError("memo maintenance rounds apply only to investment_memo run limits")
    if checked or current["schema_version"] in LIMITS_SCHEMA_VERSIONS:
        body["schema_version"] = (MEMO_MAINTENANCE_SCHEMA_VERSION
            if current["schema_version"] == MEMO_MAINTENANCE_SCHEMA_VERSION
                or MEMO_MAINTENANCE_ROUNDS_FIELD in checked else LIMITS_SCHEMA_VERSION)
        body["purpose_call_limits"] = {key: dict(value) for key, value in current.get("purpose_call_limits", {}).items()}
        body["purpose_run_limits"] = {key: dict(value) for key, value in current.get("purpose_run_limits", {}).items()}
        if checked:
            table = body[f"purpose_{kind}_limits"]
            table[purpose] = {**table.get(purpose, {}), **checked}
    semantic = lambda value: {key: item for key, item in value.items()
                              if key not in {"revision", "prior_hash", "updated_at", "actor_ref", "content_hash"}}
    if semantic(body) == semantic(current):
        return current
    body.update(revision=current["revision"] + 1, prior_hash=current["content_hash"],
                updated_at=updated_at, actor_ref=actor_ref)
    return validate_shared_call_budget_policy({**body, "content_hash": content_hash(body)})


def seed_shared_limits(policy: Mapping[str, Any], *, call_limits: Mapping[str, Any],
                       run_limits: Mapping[str, Any], actor_ref: str, updated_at: str) -> dict[str, Any]:
    """One versioned migration; existing higher shared bounds and all costs stay.

    The caller supplies reviewed effective limits from the installed consumers.
    Daily mission/pool authority is deliberately outside this policy.
    """
    current = validate_shared_call_budget_policy(policy)
    if not isinstance(call_limits, Mapping) or not isinstance(run_limits, Mapping):
        raise SharedCallBudgetPolicyError("migration limits must be objects")
    body = {key: value for key, value in current.items() if key != "content_hash"}
    body["schema_version"] = (MEMO_MAINTENANCE_SCHEMA_VERSION if current["schema_version"] == MEMO_MAINTENANCE_SCHEMA_VERSION else LIMITS_SCHEMA_VERSION)
    for kind, supplied in (("call", call_limits), ("run", run_limits)):
        name = f"purpose_{kind}_limits"
        table = {key: dict(value) for key, value in current.get(name, {}).items()}
        for purpose, fields in supplied.items():
            merged = dict(table.get(purpose, {}))
            if not isinstance(fields, Mapping):
                raise SharedCallBudgetPolicyError("migration purpose limits must be objects")
            for field, value in fields.items():
                # Validate first: bool/nonfinite must not vanish behind max().
                probe = {**body, "purpose_call_limits": {}, "purpose_run_limits": {}}
                probe[name] = {purpose: {field: value}}
                probe["content_hash"] = content_hash(probe)
                validate_shared_call_budget_policy(probe)
                merged[field] = max(merged.get(field, value), value)
            table[purpose] = merged
        body[name] = table
    semantic = lambda value: {key: item for key, item in value.items()
        if key not in {"revision", "prior_hash", "updated_at", "actor_ref", "content_hash"}}
    if semantic(body) == semantic(current):
        return current
    body.update(revision=current["revision"] + 1, prior_hash=current["content_hash"],
                updated_at=updated_at, actor_ref=actor_ref)
    return validate_shared_call_budget_policy({**body, "content_hash": content_hash(body)})
