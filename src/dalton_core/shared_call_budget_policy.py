"""Read the host-owned model call-cost policy shared by workspaces."""
from __future__ import annotations
import json
import math
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any
from .store import content_hash

SCHEMA_VERSION = "dalton-shared-call-budget-policy-0.1"
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
    if not isinstance(value, Mapping) or set(value) != _FIELDS:
        raise SharedCallBudgetPolicyError("shared call budget policy has an invalid closed shape")
    wire = dict(value); asserted = wire.pop("content_hash")
    if wire.get("schema_version") != SCHEMA_VERSION or asserted != content_hash(wire):
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
