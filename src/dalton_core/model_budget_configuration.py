"""Owner-editable call budgets for the model configuration a stage consumes."""
from __future__ import annotations

import hashlib
import fcntl
import json
import os
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from .call_budget import (CallBudgetError, default_call_budget, resolve_call_budget,
                         resolve_run_budget, validate_budget_overrides, validate_run_budget_overrides)
from .model_selection import ModelSelectionError, _write_configs_atomically, purpose_policy_bindings


class BudgetConfigurationConflict(ModelSelectionError):
    pass


def _service_budget_view(directory: Path, purpose: str, binding: Mapping[str, Any], kind: str):
    if kind != "call":
        return {"editable": False, "reason": "此服务的周期预算由治理政策配置"}
    if purpose == "plan":
        from .bounded_planner_driver import BoundedPlannerDriverConfig
        path = directory.parents[1] / "config" / "service.json"
        data = path.read_bytes()
        config = json.loads(data)
        nested = config["bounded_planner"]["config"]
        effective = dict(BoundedPlannerDriverConfig.from_mapping(nested).planner_call_budget)
        return {"editable": True, "kind": kind, "source": str(path) + "#bounded_planner.config.planner_call_budget",
                "config_path": str(path), "direct_budget_path": ["bounded_planner", "config", "planner_call_budget"],
                "overrides": validate_budget_overrides(nested.get("planner_call_budget", {})),
                "general": {}, "effective": effective, "config_hash": hashlib.sha256(data).hexdigest(),
                "requires_restart": True}
    if purpose in {"thesis_impact_assessment", "thesis_impact_verifier"}:
        from .thesis_impact_control import ASSESSMENT_BUDGET, VERIFIER_BUDGET
        path = directory / "thesis-impact-budget-config.json"
        data = path.read_bytes() if path.exists() else b""
        config = json.loads(data) if data else {}
        legacy = ASSESSMENT_BUDGET if purpose == "thesis_impact_assessment" else VERIFIER_BUDGET
        defaults = {key: legacy[key] for key in ("max_input_tokens", "max_output_tokens", "max_cost_usd")}
        defaults["timeout_seconds"] = legacy["max_seconds"]
        effective = resolve_call_budget(config, purpose, defaults=defaults)
        return {"editable": True, "kind": kind, "source": str(path), "config_path": str(path),
                "overrides": validate_budget_overrides((config.get("purpose_call_budgets") or {}).get(purpose, {})),
                "general": validate_budget_overrides(config.get("call_budget", {})),
                "effective": effective, "config_hash": hashlib.sha256(data).hexdigest(), "requires_restart": False}
    return {"editable": False, "source": binding.get("source"),
            "reason": "该环节的预算在已版本化的 Agenda policy 中配置"}


def _bindings(state_dir: Path, cockpit_model_config_path: str | Path | None = None):
    if cockpit_model_config_path is None:
        service_path = state_dir.parents[1] / "config" / "service.json"
        if service_path.is_file():
            service = json.loads(service_path.read_text())
            cockpit_model_config_path = (((service.get("control") or {}).get("config") or {})
                                         .get("cockpit") or {}).get("model_config_path")
    return purpose_policy_bindings(state_dir, cockpit_model_config_path=cockpit_model_config_path)


def _call_budget_view(state_dir: str | Path, purpose: str, *,
                     cockpit_model_config_path: str | Path | None = None,
                     binding: Mapping[str, Any] | None = None, kind: str = "call") -> dict[str, Any]:
    if kind not in {"call", "run"}:
        raise CallBudgetError("budget kind must be call or run")
    directory = Path(state_dir).expanduser().resolve()
    binding = binding or _bindings(directory, cockpit_model_config_path).get(purpose, {})
    source = binding.get("source", "")
    if binding.get("status") == "configured" and "#" in source:
        return _service_budget_view(directory, purpose, binding, kind)
    if (binding.get("status") != "configured" or not binding.get("editable")
            or "#" in source or not source):
        return {"editable": False, "source": source,
                "reason": "此环节的预算由服务或治理政策配置"}
    path = Path(source).resolve()
    if path.parent != directory:
        return {"editable": False, "source": source, "reason": "外部配置仅可查看"}
    data = path.read_bytes()
    config = json.loads(data)
    defaults_wire = json.loads(Path(__file__).with_name(f"{kind}_budget_defaults.json").read_text())
    has_defaults = purpose in defaults_wire["purposes"]
    if kind == "run" and not has_defaults:
        return {"editable": False, "source": source, "reason": "此环节没有独立的每轮预算配置"}
    validate = validate_budget_overrides if kind == "call" else validate_run_budget_overrides
    effective = ((resolve_call_budget(config, purpose, defaults=default_call_budget(purpose))
                  if kind == "call" else resolve_run_budget(config, purpose, defaults=defaults_wire["purposes"][purpose]))
                 if has_defaults else None)
    overrides = validate((config.get(f"purpose_{kind}_budgets") or {}).get(purpose, {}))
    general = validate(config.get(f"{kind}_budget", {}))
    return {"editable": True, "source": str(path), "effective": effective,
            "overrides": overrides, "general": general, "kind": kind,
            "fields": list(defaults_wire["purposes"][purpose]) if kind == "run" else None,
            "config_hash": hashlib.sha256(data).hexdigest(),
            "requires_restart": False,
            "note": "修改后用于下一次任务；mission 日预算与各预算池继续约束实际调用"}


def call_budget_view(state_dir: str | Path, purpose: str, **kwargs: Any) -> dict[str, Any]:
    from .bounded_planner_driver import BoundedPlannerDriverError
    try:
        return _call_budget_view(state_dir, purpose, **kwargs)
    except (OSError, ValueError, KeyError, TypeError, ModelSelectionError, BoundedPlannerDriverError) as exc:
        return {"editable": False, "reason": f"预算配置无法读取：{exc}"}


def _set_model_call_budget_locked(state_dir: str | Path, *, purpose: str,
                          budget: Mapping[str, Any], expected_config_hash: str,
                          actor_ref: str, kind: str = "call") -> dict[str, Any]:
    """Replace one purpose override, retaining other stages and route settings."""
    if not isinstance(actor_ref, str) or not actor_ref.startswith("human:"):
        raise CallBudgetError("only an authenticated human may configure call budgets")
    if kind not in {"call", "run"}:
        raise CallBudgetError("budget kind must be call or run")
    checked = (validate_budget_overrides if kind == "call" else validate_run_budget_overrides)(budget)
    directory = Path(state_dir).expanduser().resolve()
    view = call_budget_view(directory, purpose, kind=kind)
    if not view["editable"]:
        raise ModelSelectionError(view["reason"])
    if kind == "run" and set(checked) - set(view["fields"]):
        raise CallBudgetError("this stage does not consume the requested run budget field")
    path = Path(view.get("config_path", view["source"]))
    config = json.loads(path.read_text()) if path.exists() else {}
    budget_key = f"purpose_{kind}_budgets"
    direct_path = view.get("direct_budget_path")
    overrides = dict(config.get(budget_key) or {})
    if view["overrides"] == checked:
        return {"status": "unchanged", "purpose": purpose, **view}
    if expected_config_hash != view["config_hash"]:
        raise BudgetConfigurationConflict("model configuration changed; reload before saving the budget")
    prior = view["overrides"]
    if checked:
        overrides[purpose] = checked
    else:
        overrides.pop(purpose, None)
    if direct_path:
        updated = config
        nested = updated
        for part in direct_path[:-1]:
            nested = nested[part]
        nested[direct_path[-1]] = checked
    else:
        updated = {**config, budget_key: overrides}
    # Validate the complete map before writing a file consumed by workers.
    if direct_path:
        from .bounded_planner_driver import BoundedPlannerDriverConfig
        BoundedPlannerDriverConfig.from_mapping(updated["bounded_planner"]["config"])
    else:
        resolve_call_budget(updated, purpose, defaults=default_call_budget(purpose))
    if kind == "run":
        resolve_run_budget(updated, purpose, defaults=view["effective"])
    current = path.read_bytes() if path.exists() else b""
    if hashlib.sha256(current).hexdigest() != expected_config_hash:
        raise BudgetConfigurationConflict("model configuration changed while preparing the budget")
    receipt = {"schema_version": "0.1", "purpose": purpose, "kind": kind, "actor_ref": actor_ref,
               "created_at": datetime.now(timezone.utc).isoformat(),
               "source": str(path), "prior_config_hash": expected_config_hash,
               "prior_override": prior, "budget": checked}
    encoded = json.dumps(receipt, sort_keys=True, ensure_ascii=False).encode()
    revision = hashlib.sha256(encoded).hexdigest()
    history = directory / "model-budget-revisions"
    history.mkdir(mode=0o700, exist_ok=True)
    receipt_path = history / f"{revision}.json"
    with receipt_path.open("xb") as handle:
        os.chmod(receipt_path, 0o600)
        handle.write(encoded + b"\n")
    try:
        if path.exists():
            _write_configs_atomically([(path, updated)])
        else:
            fd, staging = tempfile.mkstemp(prefix=".budget-create-", dir=path.parent)
            try:
                with os.fdopen(fd, "w") as handle:
                    handle.write(json.dumps(updated, sort_keys=True, indent=2) + "\n")
                os.link(staging, path)
            finally:
                os.unlink(staging)
    except Exception:
        receipt_path.unlink()
        raise
    return {"status": "updated", "purpose": purpose, "revision": revision,
            **call_budget_view(directory, purpose, kind=kind)}


def set_model_call_budget(state_dir: str | Path, **values: Any) -> dict[str, Any]:
    """Serialize budget edits through the CAS, receipt and config replacement.

    The live writer already has one store executor for all owner operations;
    the file lock also protects independent callers of this configuration API.
    """
    if not isinstance(values.get("actor_ref"), str) or not values["actor_ref"].startswith("human:"):
        raise CallBudgetError("only an authenticated human may configure call budgets")
    directory = Path(state_dir).expanduser().resolve()
    fd = os.open(directory / ".model-budget-edit.lock", os.O_CREAT | os.O_RDWR, 0o600)
    with os.fdopen(fd, "a+") as handle:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX)
        try:
            return _set_model_call_budget_locked(directory, **values)
        finally:
            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)
