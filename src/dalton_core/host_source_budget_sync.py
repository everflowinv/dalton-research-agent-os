"""Follow an explicit host budget through inherited AlphaEngine ceilings.

Only the single writer applies this. Independent lower source ceilings remain
independent; immutable plans and the saved administrator instruction survive.
"""
from __future__ import annotations
import json
import os
from pathlib import Path
from .store import content_hash
from . import host_research_settings as settings

RECEIPT = 'host-source-budget-binding.json'


def _private_json(path):
    path = Path(path)
    st = path.stat()
    if path.is_symlink() or st.st_uid != os.getuid() or st.st_mode & 0o022 or st.st_size > 1_048_576:
        raise settings.HostSettingsError('预算绑定文件必须由当前用户管理')
    return json.loads(path.read_bytes())


def shared_policy_path(manager, *, persist=False):
    """Recover a missing manager pin only from unanimous strong host bindings."""
    from .workspace_manager import _config, _write
    from .shared_daily_budget import load_shared_daily_budget_policy, load_shared_daily_budget_binding
    config = _config(Path(manager))
    if config.get('shared_daily_budget_policy_path'):
        path = Path(config['shared_daily_budget_policy_path'])
        load_shared_daily_budget_policy(path)
        return path
    from .host_research_reconcile import environments
    found = []
    for environment, _ in environments(manager):
        binding = load_shared_daily_budget_binding(environment.state_dir / 'shared-daily-budget-binding.json')
        if (Path(binding['manager_config_path']).resolve() != Path(manager).resolve()
                or binding['environment_id'] != environment.environment_id):
            raise settings.HostSettingsError('共用每日预算绑定属于另一主机或项目')
        found.append(binding)
    if not found or len({str(Path(b['policy_path']).resolve()) for b in found}) != 1:
        raise settings.HostSettingsError('项目的共用每日预算绑定不一致')
    path = Path(found[0]['policy_path'])
    current = load_shared_daily_budget_policy(path)
    wanted = {b['policy_hash'] for b in found}
    for _ in range(64):
        wanted.discard(current['content_hash'])
        # A strong current record binds its immediate predecessor hash. The
        # binding already authenticates that exact predecessor at this path.
        wanted.discard(current['prior_hash'])
        if not wanted:
            break
        prior = current['prior_hash']
        if prior is None:
            raise settings.HostSettingsError('项目预算绑定不在当前策略历史链中')
        previous = load_shared_daily_budget_policy(path.parent / 'shared-daily-budget-versions' / (prior + '.json'))
        if previous['content_hash'] != prior or previous['revision'] != current['revision'] - 1:
            raise settings.HostSettingsError('共用每日预算历史链无效')
        current = previous
    else:
        raise settings.HostSettingsError('共用每日预算历史链过长')
    if persist:
        # Preserve every unrelated manager field; recheck before atomic replace.
        if _config(Path(manager)) != config:
            raise settings.HostSettingsConflict('管理配置已更新，请刷新后保存')
        _write(Path(manager), {**config, 'shared_daily_budget_policy_path': str(path)})
    return path


def _previous_cap(manager, desired):
    wanted = desired['research_budget']['max_alphaengine_calls_24h']
    prior = desired.get('prior_hash')
    seen = set()
    for _ in range(64):
        if prior is None:
            defaults = desired['creation_budget_defaults']['budget']
            return wanted if defaults is None else defaults['max_alphaengine_calls_24h']
        if prior in seen:
            raise settings.HostSettingsError('共用设置历史链存在循环')
        seen.add(prior)
        old = settings.validate(_private_json(settings.location(manager).parent / 'shared-research-settings-versions' / (prior + '.json')))
        if old['content_hash'] != prior:
            raise settings.HostSettingsError('共用设置历史绑定无效')
        budget = old['research_budget'] or old['creation_budget_defaults']['budget']
        if budget is not None and budget['max_alphaengine_calls_24h'] != wanted:
            return budget['max_alphaengine_calls_24h']
        prior = old['prior_hash']
    raise settings.HostSettingsError('共用设置历史链过长')


def _receipt(state):
    path = Path(state) / RECEIPT
    if not path.exists():
        return None
    b = _private_json(path)
    if (set(b) != {'schema_version','manager_path','desired_hash','owner_cap','owner_managed','plan_hash','plan_managed','content_hash'}
            or b['schema_version'] != 'host-source-budget-binding:0.1'
            or b['content_hash'] != content_hash({k:v for k,v in b.items() if k != 'content_hash'})):
        raise settings.HostSettingsError('来源预算继承记录无效')
    return b


def _selection(state):
    from .macos_launchagent import _alphaengine_discovery_plan
    path = _alphaengine_discovery_plan(Path(state))
    if not path.is_file():
        return None, None
    from .mission_source_discovery import load_discovery_plan
    return path, load_discovery_plan(path)


def proposal(state, manager, desired, *, plan_path=None):
    from .service_config_location import service_config_path
    from .mission_source_discovery import load_discovery_plan
    if desired['research_budget'] is None:
        return None
    cap = desired['research_budget']['max_alphaengine_calls_24h']
    if not 0 <= cap <= 2000:
        raise settings.HostSettingsError('来源调用上限必须在 0–2000 之间')
    config_path = service_config_path(state)
    config = _private_json(config_path)
    if Path(config['core_db']).resolve() != (Path(state) / 'core.sqlite').resolve():
        raise settings.HostSettingsError('来源预算配置属于另一项目')
    if plan_path is None:
        path, plan = _selection(state)
    else:
        path = Path(plan_path)
        # A blank writer receives this conventional path before a mission
        # creates any plan. Only that absent default is an unconfigured source;
        # explicit paths and existing/broken selectors remain strict.
        from .macos_launchagent import ALPHAENGINE_PLAN_SELECTOR, _alphaengine_discovery_plan
        default = (Path(state) / 'discovery-plans' / 'us-it-services-alphaengine-v1.json').resolve()
        selector = Path(state) / 'discovery-plans' / ALPHAENGINE_PLAN_SELECTOR
        missing_default = (not path.exists() and not path.is_symlink()
                           and path.resolve() == default)
        selected = _alphaengine_discovery_plan(Path(state)) if missing_default else None
        if (missing_default and selected.resolve() == default
                and not selector.exists() and not selector.is_symlink()):
            path, plan = None, None
        else:
            plan = load_discovery_plan(path)
    owner = config.get('alphaengine_owner_call_cap')
    if owner is None and plan is None:
        return None  # An installation without this source gains no authority.
    receipt = _receipt(state)
    plan_cap = None if plan is None else plan.get('budget', {}).get('max_calls_24h')
    if (receipt and receipt['desired_hash'] == desired['content_hash']
            and Path(receipt['manager_path']).resolve() == Path(manager).resolve()
            and owner == receipt['owner_cap']
            and (None if plan is None else plan['content_hash']) == receipt['plan_hash']):
        return {'config_path':config_path, 'config':config, 'owner_cap':owner,
                'owner_managed':receipt['owner_managed'], 'plan_path':path, 'plan':plan,
                'plan_managed':receipt['plan_managed'], 'next_plan':None}
    old = _previous_cap(manager, desired)
    if receipt and Path(receipt['manager_path']).resolve() != Path(manager).resolve():
        raise settings.HostSettingsError('来源预算继承记录属于另一主机')
    owner_managed = (owner == old or bool(receipt and receipt['owner_managed'] and owner == receipt['owner_cap']))
    plan_managed = (plan_cap == old or bool(receipt and receipt['plan_managed'] and plan['content_hash'] == receipt['plan_hash']))
    # Zero remains a mission/shared denial, not an invalid positive-plan or
    # positive service-config value. Preserve the last valid source ceiling.
    target_owner = cap if cap > 0 and owner_managed else owner
    next_plan = None
    if plan_managed and cap > 0 and plan_cap != cap:
        body = {k:v for k,v in plan.items() if k != 'content_hash'}
        body = json.loads(json.dumps(body))
        body['id'] = 'discovery-plan:host-budget:' + content_hash({'prior':plan['content_hash'], 'desired':desired['content_hash']})[:32]
        body['created_at'] = desired['updated_at']
        body['budget']['max_calls_24h'] = cap
        from .mission_source_discovery import validate_discovery_plan
        next_plan = validate_discovery_plan({**body, 'content_hash':content_hash(body)})
    return {'config_path':config_path, 'config':config, 'owner_cap':target_owner,
            'owner_managed':owner_managed, 'plan_path':path, 'plan':plan,
            'plan_managed':plan_managed, 'next_plan':next_plan}


def pending(state, manager, desired):
    p = proposal(state, manager, desired)
    return p is not None and (p['owner_cap'] != p['config'].get('alphaengine_owner_call_cap') or p['next_plan'] is not None)


def apply(server, manager, desired):
    p = proposal(server.state_dir, manager, desired, plan_path=getattr(server, '_discovery_plan_path', None))
    if p is None:
        return
    from .coverage_list_change import _atomic_json, selector_for, COVERAGE_SELECTOR_MARK
    path = p['plan_path']
    selector = None
    if p['next_plan'] is not None:
        plan = p['next_plan']
        directory = Path(server.state_dir) / 'discovery-plans'
        path = directory / ('alphaengine-host-budget-' + plan['content_hash'] + '.json')
        if path.exists() and _private_json(path) != plan:
            raise settings.HostSettingsError('来源预算计划版本冲突')
        if not path.exists():
            _atomic_json(path, plan)
        name, schema = selector_for(plan['source_ref'])
        selector = {'schema_version':schema,'id':f'discovery-plan-selection{COVERAGE_SELECTOR_MARK}alphaengine:host-budget:{plan["content_hash"]}',
                    'status':'approved','source_ref':plan['source_ref'],'plan_ref':plan['id'],
                    'plan_hash':plan['content_hash'],'plan_path':path.name}
        p['plan'] = plan
    # Durable inheritance intent precedes both pointer/config writes. It is
    # not an applied-status receipt: pending independently compares both live
    # fields, so a partial write cannot unlock a task or lose inheritance.
    body = {'schema_version':'host-source-budget-binding:0.1','manager_path':str(Path(manager).resolve()),
            'desired_hash':desired['content_hash'],'owner_cap':p['owner_cap'],'owner_managed':p['owner_managed'],
            'plan_hash':None if p['plan'] is None else p['plan']['content_hash'],'plan_managed':p['plan_managed']}
    receipt = {**body,'content_hash':content_hash(body)}
    if _receipt(server.state_dir) != receipt:
        settings._write(Path(server.state_dir) / RECEIPT, receipt)
    if selector is not None:
        _atomic_json(directory / name, {**selector,'content_hash':content_hash(selector)})
    if p['owner_cap'] != p['config'].get('alphaengine_owner_call_cap'):
        if _private_json(p['config_path']) != p['config']:
            raise settings.HostSettingsConflict('项目配置已更新，请重试同步')
        settings._write(p['config_path'], {**p['config'],'alphaengine_owner_call_cap':p['owner_cap']})
    server._alphaengine_owner_call_cap = p['owner_cap']
    # Existing paid children keep their frozen old plan/authorization. A
    # restarted resident with stale plist arguments adopts the saved ceiling.
    if path is not None and (str(getattr(server, '_discovery_plan_path', '')) != str(path)
            or getattr(getattr(server, '_source_discovery', None), 'owner_call_cap', None) != p['owner_cap']):
        server._install_discovery_plans(alpha_path=path)
