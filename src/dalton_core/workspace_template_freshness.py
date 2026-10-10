"""Is the runtime template a new environment is built from still today's configuration?

A new research environment is built from two immutable templates the host
manager configuration pins (``runtime_templates.model`` / ``.service``).  They
are snapshots of the mature (legacy) environment on the day they were
exported, and the owner keeps configuring that environment afterwards.  Some of
the difference is repaired at creation (``workspace_host_scheme`` re-aligns the
routing selections, the credential slots and, since 2026-09-30, the per-call
budgets); the rest -- a lane configuration the template does not carry at all
(the 22nd, ``quality-verifier-model-config.json``), a changed retry or run
budget, a changed engine setting in ``service.json`` -- is simply inherited
stale.  On 2026-09-30 the pinned model template was v5 of 09-16: 21
configurations, the document-draft stage still on the 4 096-token default the
mature environment had raised to 16 000 that morning.

This module answers "stale or not" by *hash*, the only answer that cannot miss
a field somebody forgot to list:

* **model** -- the template's lane configurations and the source environment's
  current ones are normalized the same way (the fields creation re-aligns and
  the machine-local paths removed) and hashed; a different hash is a stale
  template, and the differing fields are named;
* **service** -- ``build_service_template`` is run against the source
  environment's current ``service.json`` in memory and its ``content_hash`` is
  compared with the pinned template's.

Read-only: JSON files are parsed, nothing is opened for writing, no database is
touched.  Never raises into the caller -- a page that lists environments must
not fail because a template could not be read; it says ``unknown`` and why.
"""

from __future__ import annotations

import json
import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from .store import content_hash

SCHEMA_VERSION = "dalton-workspace-template-freshness-0.1"
#: The owner step that re-exports both templates and repoints the manager.
REEXPORT_SCRIPT = "docs/ops/reexport-runtime-templates-2026-09-30.sh"
#: Fields creation re-aligns from the source environment, or that name this
#: machine's files rather than a setting.  Everything else must match.
REALIGNED_FIELDS = frozenset({
    "routing_policy_ref", "credential_slot_refs",
    "call_budget", "purpose_call_budgets",
})
LOCAL_PATH_FIELDS = frozenset({
    "model_router_db", "budget_db", "broker_socket", "broker_auth_key",
    "shared_call_budget_policy_path",
})

_CACHE: dict[tuple[Any, ...], dict[str, Any]] = {}


def _read(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _normalized(config: Mapping[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in config.items()
            if key not in REALIGNED_FIELDS and key not in LOCAL_PATH_FIELDS}


def _mtime(path: Path) -> float | None:
    try:
        return path.stat().st_mtime
    except OSError:
        return None


def model_template_freshness(template_path: str | Path,
                             source_state_dir: str | Path) -> dict[str, Any]:
    """Compare the pinned model template with the source environment's configs."""

    template = _read(Path(template_path))
    state = Path(source_state_dir).expanduser()
    configs = template.get("configs") if isinstance(template, Mapping) else None
    if not isinstance(configs, Mapping):
        return {"status": "unknown", "reason": "模型模板读不出 configs"}
    held = {name: _normalized(value) for name, value in configs.items()
            if isinstance(value, Mapping)}
    live: dict[str, dict[str, Any]] = {}
    for path in sorted(state.glob("*model-config.json")):
        value = _read(path)
        if isinstance(value, Mapping):
            live[path.name] = _normalized(value)
    if not live:
        return {"status": "unknown", "reason": f"{state} 里没有模型配置"}
    from .workspace_model_setup import latest_budget_policy_id

    template_head = latest_budget_policy_id(template.get("budget_policies"))
    live_heads = sorted({str(value.get("budget_policy_ref")) for value in live.values()})
    # The budget pin is installed as the bundle's lineage head, so compare
    # that head, not each config's exported pin.
    for table in (held, live):
        for value in table.values():
            value.pop("budget_policy_ref", None)
    differences: list[str] = []
    for name in sorted(set(live) - set(held)):
        differences.append(f"模板缺少 {name}")
    for name in sorted(set(held) - set(live)):
        differences.append(f"模板多出 {name}（现有环境已没有）")
    for name in sorted(set(held) & set(live)):
        for key in sorted(set(held[name]) | set(live[name])):
            if held[name].get(key) != live[name].get(key):
                differences.append(f"{name} 的 {key} 与现有环境不同")
    if live_heads != [template_head]:
        differences.append(
            f"日预算策略：模板最新是 {template_head}，现有环境用 {'、'.join(live_heads)}")
    template_hash = content_hash({"configs": held, "budget_head": template_head})
    live_hash = content_hash({"configs": live, "budget_head": live_heads[0]
                              if len(live_heads) == 1 else live_heads})
    return {
        "status": "fresh" if not differences else "stale",
        "template_hash": template_hash, "source_hash": live_hash,
        "template_config_count": len(held), "source_config_count": len(live),
        "differences": differences,
    }


def service_template_freshness(template_path: str | Path,
                               source_config_path: str | Path) -> dict[str, Any]:
    """Would exporting the source's service.json today give the pinned template?"""

    from .workspace_service_setup import build_service_template

    template = _read(Path(template_path))
    rebuilt = build_service_template(source_config_path)
    pinned = template.get("content_hash") if isinstance(template, Mapping) else None
    if rebuilt["content_hash"] == pinned:
        return {"status": "fresh", "template_hash": pinned,
                "source_hash": rebuilt["content_hash"], "differences": []}
    differences = []
    operating = (template.get("operating") or {}) if isinstance(template, Mapping) else {}
    for key in sorted(set(operating) | set(rebuilt["operating"])):
        if operating.get(key) != rebuilt["operating"].get(key):
            differences.append(f"service.json 的 {key} 与现有环境不同")
    if not differences:
        differences.append("服务模板内容与现有环境导出结果不同")
    return {"status": "stale", "template_hash": pinned,
            "source_hash": rebuilt["content_hash"], "differences": differences}


def _legacy_paths(config: Mapping[str, Any]) -> tuple[Path | None, Path | None]:
    """The legacy environment's state directory and real service.json."""

    from .model_routing_sync import LEGACY_SERVICE_CANDIDATES, legacy_environment
    from .service_config_location import service_config_path

    environment = legacy_environment(config)
    if environment is None:
        return None, None
    declared = config.get("legacy_workspace") or {}
    service = declared.get("config_path") if isinstance(declared, Mapping) else None
    if isinstance(service, str) and Path(service).is_file():
        return environment.state_dir, Path(service)
    for candidate in LEGACY_SERVICE_CANDIDATES:
        path = Path(candidate).expanduser()
        if path.is_file():
            return environment.state_dir, path
    path = service_config_path(environment.state_dir)
    return environment.state_dir, (path if path.is_file() else None)


def template_freshness(config: Mapping[str, Any]) -> dict[str, Any]:
    """Both pinned templates against the legacy environment, for the create page.

    ``config`` is the parsed host manager configuration.  Cached on the
    modification times of every file read, so the environments page does not
    re-hash two dozen files on every load.
    """

    templates = config.get("runtime_templates") or {}
    if not templates:
        return {"schema_version": SCHEMA_VERSION, "status": "unknown",
                "warnings": [], "reason": "没有固定运行模板"}
    try:
        state, service = _legacy_paths(config)
    except Exception as exc:  # noqa: BLE001 - never break the listing
        return {"schema_version": SCHEMA_VERSION, "status": "unknown",
                "warnings": [], "reason": f"找不到现有研究环境：{exc}"}
    if state is None:
        return {"schema_version": SCHEMA_VERSION, "status": "unknown",
                "warnings": [], "reason": "这台机器上没有现有研究环境可比对"}
    model_path = Path(templates["model"]["path"]) if "model" in templates else None
    service_path = Path(templates["service"]["path"]) if "service" in templates else None
    watched = [model_path, service_path, service, *sorted(state.glob("*model-config.json"))]
    key = tuple((str(path), _mtime(path)) for path in watched if path is not None)
    if key in _CACHE:
        return _CACHE[key]
    result: dict[str, Any] = {"schema_version": SCHEMA_VERSION,
                              "source_state_dir": str(state), "fix": REEXPORT_SCRIPT}
    for name, check in (
            ("model", lambda: model_template_freshness(model_path, state)
             if model_path is not None else {"status": "unknown", "reason": "未配置"}),
            ("service", lambda: service_template_freshness(service_path, service)
             if service_path is not None and service is not None
             else {"status": "unknown", "reason": "未配置或找不到现有环境的 service.json"})):
        try:
            result[name] = check()
        except Exception as exc:  # noqa: BLE001 - one template, not the page
            result[name] = {"status": "unknown", "reason": f"{type(exc).__name__}: {exc}"}
    statuses = {result["model"]["status"], result["service"]["status"]}
    result["status"] = ("stale" if "stale" in statuses
                        else "fresh" if statuses == {"fresh"} else "unknown")
    warnings: list[str] = []
    for name, label in (("model", "模型模板"), ("service", "服务模板")):
        section = result[name]
        if section["status"] == "stale":
            listed = section["differences"][:3]
            more = len(section["differences"]) - len(listed)
            warnings.append(f"{label}比现有环境旧：" + "；".join(listed)
                            + (f"；另有 {more} 处" if more > 0 else ""))
    if warnings:
        warnings.append(f"新环境会继承这些旧设置；请先运行 {REEXPORT_SCRIPT} 重新导出模板。")
    result["warnings"] = warnings
    if len(_CACHE) > 16:
        _CACHE.clear()
    _CACHE[key] = result
    return result


def clear_cache() -> None:
    _CACHE.clear()


__all__ = ["REEXPORT_SCRIPT", "SCHEMA_VERSION", "clear_cache",
           "model_template_freshness", "service_template_freshness",
           "template_freshness"]

# ``os`` is kept imported for callers that patch environment lookups in tests.
del os
