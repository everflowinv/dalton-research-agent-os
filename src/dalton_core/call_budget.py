"""Pure resolution of per-call model budgets from an installed configuration."""

from __future__ import annotations

import hashlib
import json
import math
import re
from collections.abc import Mapping
from pathlib import Path
from typing import Any

FIELDS = ("max_input_tokens", "max_output_tokens", "max_cost_usd", "timeout_seconds")
RUN_FIELDS = ("max_cost_usd", "max_units", "max_events",
              "max_events_per_company", "max_calls")
_PURPOSE = re.compile(r"^[a-z][a-z0-9]*(?:_[a-z0-9]+)*$")


class CallBudgetError(ValueError):
    pass


def _checked(value: Any, name: str) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise CallBudgetError(f"{name} must be an object")
    unknown = sorted(set(value) - set(FIELDS))
    if unknown:
        raise CallBudgetError(f"{name} has unknown fields: {unknown}")
    out = dict(value)
    for field in ("max_input_tokens", "max_output_tokens", "timeout_seconds"):
        if field in out and (isinstance(out[field], bool) or not isinstance(out[field], int)
                             or out[field] <= 0):
            raise CallBudgetError(f"{name}.{field} must be a positive integer")
    if "max_cost_usd" in out:
        cost = out["max_cost_usd"]
        if (isinstance(cost, bool) or not isinstance(cost, (int, float))
                or not math.isfinite(float(cost)) or float(cost) <= 0):
            raise CallBudgetError(f"{name}.max_cost_usd must be a positive finite number")
    return out


def validate_budget_overrides(value: Any) -> dict[str, Any]:
    """Validate and copy a partial owner override without inventing defaults."""
    return _checked(value, "call budget override")


def validate_run_budget_overrides(value: Any) -> dict[str, Any]:
    """Validate a partial multi-unit run bound without supplying policy."""
    if not isinstance(value, Mapping):
        raise CallBudgetError("run budget override must be an object")
    unknown = sorted(set(value) - set(RUN_FIELDS))
    if unknown:
        raise CallBudgetError(f"run budget override has unknown fields: {unknown}")
    out = dict(value)
    for field in RUN_FIELDS[1:]:
        if field in out and (isinstance(out[field], bool) or not isinstance(out[field], int)
                             or out[field] <= 0):
            raise CallBudgetError(f"run budget override.{field} must be a positive integer")
    if "max_cost_usd" in out:
        cost = out["max_cost_usd"]
        if (isinstance(cost, bool) or not isinstance(cost, (int, float))
                or not math.isfinite(float(cost)) or float(cost) <= 0):
            raise CallBudgetError(
                "run budget override.max_cost_usd must be a positive finite number")
    return out


def resolve_run_budget(config: Mapping[str, Any], purpose: str, *,
                       defaults: Mapping[str, Any]) -> dict[str, Any]:
    """Return purpose > general > caller defaults for a bounded multi-unit run."""
    if not isinstance(config, Mapping):
        raise CallBudgetError("model configuration must be an object")
    if not isinstance(purpose, str) or not _PURPOSE.fullmatch(purpose):
        raise CallBudgetError("purpose must be a canonical token")
    resolved = default_run_budget(purpose, defaults=defaults)
    if not resolved:
        raise CallBudgetError("run budget defaults must not be empty")
    resolved.update(validate_run_budget_overrides(config.get("run_budget", {})))
    per_purpose = config.get("purpose_run_budgets", {})
    if not isinstance(per_purpose, Mapping):
        raise CallBudgetError("purpose_run_budgets must be an object")
    for key, value in per_purpose.items():
        if not isinstance(key, str) or not _PURPOSE.fullmatch(key):
            raise CallBudgetError("purpose_run_budgets keys must be canonical purpose tokens")
        validate_run_budget_overrides(value)
    resolved.update(validate_run_budget_overrides(per_purpose.get(purpose, {})))
    return resolved


def default_run_budget(purpose: str, *,
                       defaults: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Read packaged run bounds, preserving unlisted callers' own defaults."""
    if not isinstance(purpose, str) or not _PURPOSE.fullmatch(purpose):
        raise CallBudgetError("purpose must be a canonical token")
    path = Path(__file__).with_name("run_budget_defaults.json")
    if path.is_file():
        try:
            wire = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise CallBudgetError(f"packaged run budget defaults cannot be read: {exc}") from exc
        if (not isinstance(wire, Mapping) or set(wire) != {"schema_version", "purposes"}
                or wire.get("schema_version") != "0.1"
                or not isinstance(wire.get("purposes"), Mapping)):
            raise CallBudgetError("packaged run budget defaults have an invalid shape")
        for key, value in wire["purposes"].items():
            if not isinstance(key, str) or not _PURPOSE.fullmatch(key):
                raise CallBudgetError("packaged run purpose keys must be canonical tokens")
            validate_run_budget_overrides(value)
        if purpose in wire["purposes"]:
            resolved = ({} if defaults is None
                        else validate_run_budget_overrides(defaults))
            resolved.update(validate_run_budget_overrides(wire["purposes"][purpose]))
        elif defaults is not None:
            resolved = validate_run_budget_overrides(defaults)
        else:
            raise CallBudgetError(f"no packaged run budget for purpose {purpose}")
    elif defaults is not None:
        resolved = validate_run_budget_overrides(defaults)
    else:
        raise CallBudgetError("packaged run budget defaults are missing")
    if not resolved:
        raise CallBudgetError("run budget defaults must not be empty")
    return resolved


def default_call_budget(purpose: str, *,
                        defaults: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Read the packaged baseline, with a caller fallback for old installations."""
    if not isinstance(purpose, str) or not _PURPOSE.fullmatch(purpose):
        raise CallBudgetError("purpose must be a canonical token")
    path = Path(__file__).with_name("call_budget_defaults.json")
    if path.is_file():
        try:
            wire = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise CallBudgetError(f"packaged call budget defaults cannot be read: {exc}") from exc
        if (not isinstance(wire, Mapping) or set(wire) != {"schema_version", "defaults", "purposes"}
                or wire.get("schema_version") != "0.1"
                or not isinstance(wire.get("purposes"), Mapping)):
            raise CallBudgetError("packaged call budget defaults have an invalid shape")
        packaged = _checked(wire["defaults"], "packaged defaults")
        for key, value in wire["purposes"].items():
            if not isinstance(key, str) or not _PURPOSE.fullmatch(key):
                raise CallBudgetError("packaged purpose keys must be canonical tokens")
            _checked(value, f"packaged purposes.{key}")
        if purpose in wire["purposes"]:
            base = packaged
            base.update(_checked(wire["purposes"][purpose],
                                 f"packaged purposes.{purpose}"))
        elif defaults is not None:
            # An unlisted existing caller remains authoritative for its own
            # legacy defaults. The catalog base is for read-only discovery,
            # not a reason to silently change a runtime contract.
            base = _checked(defaults, "defaults")
        else:
            base = packaged
    elif defaults is not None:
        base = _checked(defaults, "defaults")
    else:
        raise CallBudgetError("packaged call budget defaults are missing")
    missing = sorted(set(FIELDS) - set(base))
    if missing:
        raise CallBudgetError(f"call budget defaults are missing fields: {missing}")
    return {field: base[field] for field in FIELDS}


def resolve_call_budget(config: Mapping[str, Any], purpose: str, *,
                        defaults: Mapping[str, Any]) -> dict[str, Any]:
    """Return purpose > general > caller defaults, after closed validation."""
    if not isinstance(config, Mapping):
        raise CallBudgetError("model configuration must be an object")
    if not isinstance(purpose, str) or not _PURPOSE.fullmatch(purpose):
        raise CallBudgetError("purpose must be a canonical token")
    resolved = default_call_budget(purpose, defaults=defaults)
    resolved.update(_checked(config.get("call_budget", {}), "call_budget"))
    per_purpose = config.get("purpose_call_budgets", {})
    if not isinstance(per_purpose, Mapping):
        raise CallBudgetError("purpose_call_budgets must be an object")
    for key, value in per_purpose.items():
        if not isinstance(key, str) or not _PURPOSE.fullmatch(key):
            raise CallBudgetError("purpose_call_budgets keys must be canonical purpose tokens")
        _checked(value, f"purpose_call_budgets.{key}")
    resolved.update(_checked(per_purpose.get(purpose, {}),
                             f"purpose_call_budgets.{purpose}"))
    return {field: resolved[field] for field in FIELDS}


def budget_fingerprint(budget: Mapping[str, Any]) -> str:
    checked = _checked(budget, "budget")
    if set(checked) != set(FIELDS):
        raise CallBudgetError("budget fingerprint requires all four fields")
    wire = json.dumps({field: checked[field] for field in FIELDS},
                      sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(wire.encode("utf-8")).hexdigest()


__all__ = ["CallBudgetError", "budget_fingerprint", "default_call_budget", "default_run_budget",
           "resolve_call_budget", "resolve_run_budget", "validate_budget_overrides",
           "validate_run_budget_overrides"]
