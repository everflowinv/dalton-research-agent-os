"""Durable host model preferences and explicitly saved research limits.

The desired record is the user instruction. It is committed before fan-out;
individual environments retain their own immutable writer authorities.
"""
from __future__ import annotations

import fcntl
import json
import math
import os
import re
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from .store import content_hash

LEGACY_SCHEMA_VERSION = "dalton-host-research-settings-0.1"
SCHEMA_VERSION = "dalton-host-research-settings-0.2"
FILENAME = "shared-research-settings.json"
HEX64 = re.compile(r"[0-9a-f]{64}")


class HostSettingsError(ValueError):
    pass


class HostSettingsConflict(HostSettingsError):
    pass


def location(manager_config: str | Path) -> Path:
    from .workspace_manager import _config
    config = _config(Path(manager_config))
    return Path(config["host_root"]).resolve() / FILENAME


def validate_budget(value: Any) -> dict[str, Any]:
    required = {"max_daily_paid_calls", "max_daily_cost_usd", "max_alphaengine_calls_24h"}
    optional = {"pools_enforcement", "max_daily_document_reads", "material_acquisition"}
    if not isinstance(value, Mapping) or not required <= value.keys() or set(value) - required - optional:
        raise HostSettingsError("研究预算字段不完整")
    for name in required - {"max_daily_cost_usd"} | (optional & set(value) - {"pools_enforcement", "material_acquisition"}):
        number = value[name]
        if type(number) is not int or number < 0:
            raise HostSettingsError("研究次数上限必须是零或正整数")
    if value["max_alphaengine_calls_24h"] > 2000:
        raise HostSettingsError("来源调用上限必须在 0–2000 之间")
    cost = value["max_daily_cost_usd"]
    if isinstance(cost, bool) or not isinstance(cost, (int, float)) or not math.isfinite(cost) or cost <= 0 or round(cost * 1_000_000) <= 0:
        raise HostSettingsError("每天总费用上限必须大于零")
    if value.get("pools_enforcement", "on") not in {"on", "off"}:
        raise HostSettingsError("分项预算池只接受 on 或 off")
    if "material_acquisition" in value:
        from .budget_pools import validate_material_acquisition_caps, BudgetPoolError
        try:
            validate_material_acquisition_caps(value["material_acquisition"])
        except BudgetPoolError as exc:
            raise HostSettingsError("材料获取上限必须是完整、有效的次数和美元额度") from exc
    return dict(value)


def mission_budget(value):
    """Connector-pool settings do not rewrite a mission or its paid identity."""
    return {k: v for k, v in value.items() if k != "material_acquisition"}


def validate_selection(params: Mapping[str, Any]) -> dict[str, Any]:
    from .model_selection import PURPOSE_LABELS, SELECTION_MODES
    from .model_fallback_chain import purpose_tiers
    from .call_budget import resolve_thinking_level
    if not isinstance(params, Mapping):
        raise HostSettingsError("模型设置必须是一个对象")
    if "thinking_level" in params:
        if set(params) != {"purpose", "thinking_level", "reason"} or params["purpose"] not in PURPOSE_LABELS:
            raise HostSettingsError("思考强度必须按已登记的环节调整")
        if not isinstance(params["reason"], str) or not params["reason"].strip() or len(params["reason"]) > 300:
            raise HostSettingsError("调整思考强度需填写理由")
        result = dict(params)
        result["thinking_level"] = resolve_thinking_level(params["purpose"], requested=params["thinking_level"])
        return result
    kind = "tier" if "tier" in params else "purpose"
    if set(params) - {kind, "mode", "chain"} or not {kind, "mode"} <= params.keys():
        raise HostSettingsError("模型设置字段不完整")
    allowed = set(purpose_tiers().values()) if kind == "tier" else set(PURPOSE_LABELS)
    if params[kind] not in allowed or params["mode"] not in SELECTION_MODES:
        raise HostSettingsError("请选择已登记的任务类别或环节")
    chain = params.get("chain", [])
    if not isinstance(chain, list) or len(chain) > 32 or any(not isinstance(p, str) or not p.startswith("profile:") or len(p) > 256 or re.search(r"\s", p) for p in chain) or len(set(chain)) != len(chain):
        raise HostSettingsError("模型顺序必须是无重复的已登记模型列表")
    if params["mode"] == "explicit" and not chain:
        raise HostSettingsError("手动顺序至少保留一个模型")
    if params["mode"] == "tier" and chain:
        raise HostSettingsError("默认顺序不能同时指定模型")
    return {kind: params[kind], "mode": params["mode"], **({"chain": list(chain)} if chain else {})}


def validate(raw: Any) -> dict[str, Any]:
    fields = {"schema_version", "revision", "prior_hash", "updated_at", "actor_ref", "selections", "research_budget", "creation_budget_defaults", "content_hash"}
    if not isinstance(raw, dict) or set(raw) != fields:
        raise HostSettingsError("共用设置记录格式无效")
    body = {k: v for k, v in raw.items() if k != "content_hash"}
    if raw["schema_version"] not in {LEGACY_SCHEMA_VERSION, SCHEMA_VERSION} or raw["content_hash"] != content_hash(body):
        raise HostSettingsError("共用设置记录校验失败")
    if type(raw["revision"]) is not int or raw["revision"] < 1 or (raw["prior_hash"] is not None and not HEX64.fullmatch(str(raw["prior_hash"]))):
        raise HostSettingsError("共用设置版本无效")
    if not isinstance(raw["actor_ref"], str) or not re.fullmatch(r"human:[A-Za-z0-9._-]+", raw["actor_ref"]):
        raise HostSettingsError("共用设置需要已授权的人工或共享主体")
    try:
        stamp = datetime.fromisoformat(raw["updated_at"].replace("Z", "+00:00"))
        if stamp.tzinfo is None:
            raise ValueError()
    except (TypeError, ValueError, AttributeError) as exc:
        raise HostSettingsError("共用设置保存时间无效") from exc
    selections = raw["selections"]
    if not isinstance(selections, dict) or set(selections) != {"tiers", "purposes", "thinking"}:
        raise HostSettingsError("共用模型设置格式无效")
    for key, kind in (("tiers", "tier"), ("purposes", "purpose")):
        if not isinstance(selections[key], dict):
            raise HostSettingsError("共用模型设置格式无效")
        for name, entry in selections[key].items():
            if not isinstance(entry, dict):
                raise HostSettingsError("共用模型设置格式无效")
            validate_selection({kind: name, **entry})
    if not isinstance(selections["thinking"], dict):
        raise HostSettingsError("共用思考强度设置格式无效")
    for name, entry in selections["thinking"].items():
        if not isinstance(entry, dict) or set(entry) != {"level", "reason"}:
            raise HostSettingsError("共用思考强度设置格式无效")
        validate_selection({"purpose": name, "thinking_level": entry["level"], "reason": entry["reason"]})
    defaults = raw["creation_budget_defaults"]
    if not isinstance(defaults, dict) or set(defaults) != {"budget", "basis", "source_bindings"} or defaults["basis"] not in {"held_consensus", "explicit_global_save", "unavailable"} or not isinstance(defaults["source_bindings"], list):
        raise HostSettingsError("新项目预算默认值来源无效")
    if defaults["budget"] is not None:
        validate_budget(defaults["budget"])
    for binding in defaults["source_bindings"]:
        if (not isinstance(binding, dict) or set(binding) != {"environment_id", "mission_ref", "mission_hash"}
                or not isinstance(binding["environment_id"], str) or not binding["environment_id"]
                or not isinstance(binding["mission_ref"], str) or not binding["mission_ref"].startswith("coverage-mission:")
                or not HEX64.fullmatch(str(binding["mission_hash"]))):
            raise HostSettingsError("新项目预算默认值来源无效")
    if raw["research_budget"] is not None:
        validate_budget(raw["research_budget"])
    if raw["schema_version"] == LEGACY_SCHEMA_VERSION and any(
            b is not None and "material_acquisition" in b
            for b in (raw["research_budget"], defaults["budget"])):
        raise HostSettingsError("材料获取设置需要新版共用预算合同")
    return dict(raw)


def load(manager_config: str | Path) -> dict[str, Any] | None:
    path = location(manager_config)
    if not path.exists():
        return None
    if path.is_symlink() or path.stat().st_uid != os.getuid() or path.stat().st_mode & 0o022:
        raise HostSettingsError("共用设置必须由当前用户管理")
    return validate(json.loads(path.read_bytes()))


def seed_selections(state_dir: str | Path) -> dict[str, Any]:
    from .planner_paid_output_recovery import _authority_connection
    from .store import canonical_json
    state = Path(state_dir)
    with _authority_connection(state / 'model-router.sqlite') as connection:
        rows = connection.execute("SELECT p.policy_json,p.policy_hash AS content_hash FROM model_routing_policy_versions p JOIN "
            "(SELECT policy_id,MAX(version) AS v FROM model_routing_policy_versions GROUP BY policy_id) h "
            "ON p.policy_id=h.policy_id AND p.version=h.v").fetchall()
    if not rows:
        raise HostSettingsError('当前研究项目没有已登记的模型策略')
    result = {'tiers': {}, 'purposes': {}, 'thinking': {}}
    for row in rows:
        policy = json.loads(row['policy_json'])
        if (canonical_json(policy) != row['policy_json'] or policy.get('content_hash') != row['content_hash']
                or content_hash({k:v for k,v in policy.items() if k!='content_hash'}) != row['content_hash']):
            raise HostSettingsError('当前模型策略内容校验失败')
        groups = {'tiers': {k:{'mode':'explicit','chain':v} for k,v in
                            (policy.get('fallback_chains') or {}).get('tiers',{}).items()
                            if k!='legacy_default' and not k.startswith('saved_default_') and v},
                  'purposes': {k:{name:v[name] for name in ('mode','chain') if name in v} for k,v in (policy.get('purpose_overrides') or {}).items()},
                  'thinking': {k:{name:v[name] for name in ('level','reason')} for k,v in (policy.get('thinking_level_overrides') or {}).items()}}
        for group, values in groups.items():
            for subject, value in values.items():
                if subject in result[group] and result[group][subject] != value:
                    raise HostSettingsError('当前模型设置存在差异，请先核对共用顺序')
                result[group][subject] = value
    return result


def current_mission(state_dir: str | Path) -> dict[str, Any] | None:
    """Strong read only; the initializer never constructs a write authority."""
    from .coverage_mission import CoverageMissionAuthority
    from .planner_paid_output_recovery import _authority_connection
    database = Path(state_dir) / "core.sqlite"
    if not database.is_file():
        return None
    with _authority_connection(database) as connection:
        connection.execute("PRAGMA query_only=ON")
        row = connection.execute("SELECT mission_ref FROM coverage_mission_pointer ORDER BY updated_at DESC LIMIT 1").fetchone()
        if row is None:
            return None
        authority = object.__new__(CoverageMissionAuthority)
        authority.connection = connection
        return authority.active_mission(row["mission_ref"])


def creation_defaults(manager_config: str | Path) -> dict[str, Any]:
    from .host_research_reconcile import environments
    fields = {"max_daily_paid_calls", "max_daily_cost_usd", "max_alphaengine_calls_24h", "max_daily_document_reads"}
    budget = None
    bindings = []
    for environment, _ in environments(manager_config):
        mission = current_mission(environment.state_dir)
        if mission is None:
            continue
        held = {k: v for k, v in mission["budget"].items() if k in fields}
        validate_budget(held)
        if budget is not None and held != budget:
            raise HostSettingsError("现有研究项目的共同预算上限不同，请先明确全局上限")
        budget = held
        bindings.append({"environment_id": environment.environment_id, "mission_ref": mission["mission_ref"], "mission_hash": mission["content_hash"]})
    return {"budget": budget, "basis": "unavailable" if budget is None else "held_consensus", "source_bindings": bindings}


def defaults_for_workspace(state_dir: str | Path) -> tuple[dict[str, Any], str | None]:
    from .workspace_host_scheme import resolve_manager_config
    manager = resolve_manager_config(state_dir)
    if manager is None:
        return {}, None
    desired = load(manager)
    if desired is None:
        return {}, None
    defaults = desired["creation_budget_defaults"]["budget"]
    return ({} if defaults is None else mission_budget(defaults)), desired["content_hash"]


def _write(path: Path, wire: Mapping[str, Any]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".host-settings-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write((json.dumps(wire, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode())
            stream.flush(); os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        Path(temporary).unlink(missing_ok=True)


def save(manager_config: str | Path, *, actor_ref: str, expected_hash: str | None,
         selection: Mapping[str, Any] | None = None, research_budget: Mapping[str, Any] | None = None,
         source_state_dir: str | Path | None = None) -> dict[str, Any]:
    if selection is not None and research_budget is not None:
        raise HostSettingsError("请分别保存模型设置与研究预算")
    selected = None if selection is None else validate_selection(selection)
    if selected is not None and 'thinking_level' not in selected:
        if source_state_dir is None:
            raise HostSettingsError('保存模型顺序需要当前项目配置')
        from .model_router import ModelRouter
        with ModelRouter(Path(source_state_dir) / 'model-router.sqlite', read_only=True) as router:
            from .model_selection import validate_selection as validate_model_selection
            from .model_fallback_chain import purpose_tiers
            purpose = selected.get('purpose') or next(name for name, tier in purpose_tiers().items() if tier == selected['tier'])
            validate_model_selection(router, purpose=purpose, mode=selected['mode'], chain=selected.get('chain', []))
    budget = None if research_budget is None else validate_budget(research_budget)
    if budget is not None:
        from .host_source_budget_sync import shared_policy_path
        shared_policy_path(manager_config)
    path = location(manager_config); path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    lock = path.with_suffix(".lock")
    if lock.is_symlink():
        raise HostSettingsError("共用设置锁无效")
    with lock.open("a+") as stream:
        os.chmod(lock, 0o600); fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        prior = load(manager_config)
        if expected_hash != (None if prior is None else prior["content_hash"]):
            raise HostSettingsConflict("共用设置已更新，请刷新后再保存")
        if prior is None:
            if source_state_dir is None:
                raise HostSettingsError("首次保存需读取当前已登记的模型配置")
            from .host_research_reconcile import environments
            from .model_routing_sync import environment_for_state_dir
            if environment_for_state_dir([row[0] for row in environments(manager_config)], source_state_dir) is None:
                raise HostSettingsError("模型配置不属于此研究主机")
            selections = seed_selections(source_state_dir)
            for environment, _ in environments(manager_config):
                if environment.router_db.is_file() and seed_selections(environment.state_dir) != selections:
                    raise HostSettingsError('现有研究项目的模型设置不同，请先明确共用顺序')
            old_budget = None
            defaults = creation_defaults(manager_config)
        else:
            selections = json.loads(json.dumps(prior["selections"])); old_budget = prior["research_budget"]
            defaults = prior["creation_budget_defaults"]
        if selected is not None:
            if "thinking_level" in selected:
                selections["thinking"][selected["purpose"]] = {"level": selected["thinking_level"], "reason": selected["reason"]}
            else:
                kind = "tiers" if "tier" in selected else "purposes"; name = selected.get("tier", selected.get("purpose"))
                selections[kind][name] = {k: v for k, v in selected.items() if k not in {"tier", "purpose"}}
        if budget is not None and "material_acquisition" not in budget and old_budget is not None and "material_acquisition" in old_budget:
            budget = {**budget, "material_acquisition": old_budget["material_acquisition"]}
        desired_budget = old_budget if budget is None else budget
        if budget is not None:
            defaults = {"budget": dict(budget), "basis": "explicit_global_save", "source_bindings": []}
        if prior is not None and selections == prior["selections"] and desired_budget == old_budget:
            return {"status": "unchanged", "desired": prior}
        body = {"schema_version": SCHEMA_VERSION, "revision": 1 if prior is None else prior["revision"] + 1,
                "prior_hash": None if prior is None else prior["content_hash"],
                "updated_at": datetime.now(timezone.utc).isoformat(), "actor_ref": actor_ref,
                "selections": selections, "research_budget": desired_budget, "creation_budget_defaults": defaults}
        wire = validate({**body, "content_hash": content_hash(body)})
        history = path.parent / "shared-research-settings-versions" / (wire["content_hash"] + ".json")
        if history.exists():
            if validate(json.loads(history.read_bytes())) != wire:
                raise HostSettingsError("共用设置历史版本冲突")
        else:
            _write(history, wire)
        _write(path, wire)
        if budget is not None:
            from .host_research_reconcile import _sync_shared
            _sync_shared(manager_config, wire)
        return {"status": "saved", "desired": wire}


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(description='Seed durable global settings from current strong authorities; no fanout.')
    parser.add_argument('--manager-config', required=True, type=Path)
    parser.add_argument('--source-state-dir', required=True, type=Path)
    parser.add_argument('--actor-ref', required=True)
    parser.add_argument('--expected-hash')
    parser.add_argument('--apply', action='store_true')
    args = parser.parse_args(argv)
    if not args.apply:
        prior = load(args.manager_config)
        result = {'status': 'preview', 'expected_hash': None if prior is None else prior['content_hash'],
                  'selections': seed_selections(args.source_state_dir),
                  'creation_budget_defaults': creation_defaults(args.manager_config),
                  'research_budget': None, 'fanout': False}
    else:
        result = save(args.manager_config, actor_ref=args.actor_ref, expected_hash=args.expected_hash,
                      source_state_dir=args.source_state_dir)
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
