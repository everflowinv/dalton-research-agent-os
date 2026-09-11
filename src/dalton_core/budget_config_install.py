"""Preserve validated owner budget overrides when regenerating model configs."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .call_budget import validate_budget_overrides, validate_run_budget_overrides

BUDGET_KEYS = ("call_budget", "purpose_call_budgets", "run_budget", "purpose_run_budgets",
               "capacity_retry", "transport_retry", "provider_retry", "reading_limits")


class BudgetConfigInstallError(ValueError):
    pass


def preserved_budget_overrides(path: str | Path) -> dict[str, Any]:
    """Preserve supported execution controls; malformed blocks fail closed."""

    target = Path(path)
    if not target.exists():
        return {}
    try:
        wire = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise BudgetConfigInstallError(f"existing model config cannot be read: {exc}") from exc
    if not isinstance(wire, Mapping):
        raise BudgetConfigInstallError("existing model config must be an object")
    kept: dict[str, Any] = {}
    if "capacity_retry" in wire:
        from .document_extraction import validate_model_config
        # Reuse the closed model-config validator without accepting only part
        # of a malformed recovery policy.
        validate_model_config(wire)
        kept["capacity_retry"] = dict(wire["capacity_retry"])
    if "reading_limits" in wire:
        from .document_reading_limits import resolve_reading_limits
        resolve_reading_limits(wire)
        kept["reading_limits"] = dict(wire["reading_limits"])
    if "transport_retry" in wire:
        from .document_extraction import validate_transport_retry
        kept["transport_retry"] = validate_transport_retry(wire["transport_retry"])
    if "provider_retry" in wire:
        from .provider_retry import validate_provider_retry
        kept["provider_retry"] = validate_provider_retry(wire["provider_retry"])
    for key in ("call_budget", "run_budget"):
        if key in wire:
            validator = validate_budget_overrides if key == "call_budget" else validate_run_budget_overrides
            kept[key] = validator(wire[key])
    for key in ("purpose_call_budgets", "purpose_run_budgets"):
        if key not in wire:
            continue
        raw = wire[key]
        if not isinstance(raw, Mapping):
            raise BudgetConfigInstallError(f"existing {key} must be an object")
        validator = (validate_budget_overrides if key == "purpose_call_budgets"
                     else validate_run_budget_overrides)
        kept[key] = {purpose: validator(value) for purpose, value in raw.items()}
    return kept


__all__ = ["BUDGET_KEYS", "BudgetConfigInstallError", "preserved_budget_overrides"]
