"""Replay a durable administrator instruction through each environment's writer.

No budget or model authority is copied between databases. The saved actor remains
its actual human/shared subject; each writer publishes its ordinary local versions.
"""
from __future__ import annotations
import fcntl
import json
import os
import sqlite3
from pathlib import Path
from typing import Any, Mapping
from . import host_research_settings as settings


def environments(manager_config):
    from .workspace_manager import _config
    from .workspace import load_workspace_manifest
    from .model_routing_sync import legacy_environment, _environment, _workspace_name
    config = _config(Path(manager_config))
    found = []
    legacy = legacy_environment(config)
    if legacy is not None:
        found.append((legacy, 'active'))
    for path in sorted((Path(config['host_root']) / 'workspaces').glob('*/workspace.json')):
        workspace = load_workspace_manifest(path)
        if workspace.lifecycle not in {'active', 'paused'}:
            continue
        found.append((_environment(environment_id=workspace.workspace_id, slug=workspace.slug,
                     name=_workspace_name(workspace.workspace_root, workspace.slug),
                     state_dir=workspace.state_dir, writer_socket=workspace.writer_socket), workspace.lifecycle))
    return found


def selections(desired):
    for key, name in (('tiers', 'tier'), ('purposes', 'purpose')):
        for subject, entry in sorted(desired['selections'][key].items()):
            yield {name: subject, **entry}
    for subject, entry in sorted(desired['selections']['thinking'].items()):
        yield {'purpose': subject, 'thinking_level': entry['level'], 'reason': entry['reason']}


def _consumer_policy_bindings(environment):
    from .model_selection import model_configs, _runtime_policy_config, RETIRED_PURPOSES
    from .model_router import ModelRouter
    from .store import content_hash
    pins = [(pin, None) for pin in model_configs(environment.state_dir)]
    for purpose in ('plan', 'human_intent', 'agenda_planning', 'thesis_impact_assessment', 'thesis_impact_verifier'):
        if purpose in RETIRED_PURPOSES:
            continue
        runtime = _runtime_policy_config(environment.state_dir, purpose)
        if runtime is not None:
            pins.append((runtime, purpose))
    policies = {}
    bindings = []
    for pin, purpose in pins:
        config = pin.get('runtime_config', pin['config'])
        router_db = pin.get('router_db', config.get('model_router_db'))
        ref = config[pin.get('field', 'routing_policy_ref')]
        key = (router_db, ref)
        if key not in policies:
            from .planner_paid_output_recovery import _authority_connection
            with _authority_connection(router_db) as connection:
                router = object.__new__(ModelRouter); router.connection = connection
                policy = router.get_policy(ref)
            if policy.get('content_hash') != content_hash({k: v for k, v in policy.items() if k != 'content_hash'}):
                raise settings.HostSettingsError('模型策略内容校验失败')
            policies[key] = policy
        bindings.append((policies[key], purpose))
    return bindings


def consumer_policies(environment):
    policies = {}
    for policy, _ in _consumer_policy_bindings(environment):
        policies[policy["policy_version_ref"]] = policy
    return list(policies.values())


def policy_matches(policy, params):
    from .model_fallback_chain import tier_chain
    if params.get('thinking_level') is not None:
        entry = (policy.get('thinking_level_overrides') or {}).get(params['purpose'], {})
        return all(entry.get(k) == value for k, value in
                   {'level': params['thinking_level'], 'reason': params['reason']}.items())
    if 'tier' in params:
        wanted = params.get('chain') if params['mode'] == 'explicit' else list(tier_chain(params['tier']))
        return (policy.get('fallback_chains') or {}).get('tiers', {}).get(params['tier']) == wanted
    entry = (policy.get('purpose_overrides') or {}).get(params['purpose'], {})
    return (entry.get('mode', 'tier') == params['mode']
            and (params['mode'] != 'explicit' or entry.get('chain') == params['chain']))


def selection_landed(environment, params, bindings=None):
    # Registered lane configs are repointed by every normal selection save.
    # A resident service pin is repointed only for its own purpose/tier.
    # Check that exact scope rather than requiring unrelated runtime pins to
    # carry selections that the ordinary writer intentionally did not publish.
    from .model_fallback_chain import tier_for
    bindings = _consumer_policy_bindings(environment) if bindings is None else bindings
    policies = [policy for policy, purpose in bindings if purpose is None or
                (tier_for(purpose) == params['tier'] if 'tier' in params else purpose == params['purpose'])]
    return bool(policies) and all(policy_matches(policy, params) for policy in policies)


def budget_landed(mission, budget):
    return mission is not None and all(mission['budget'].get(k) == v for k, v in settings.mission_budget(budget).items())


def _shared_landed(manager, desired):
    if desired['research_budget'] is None:
        return True
    from .workspace_manager import _config
    from .shared_daily_budget import load_shared_daily_budget_policy
    from .host_source_budget_sync import shared_policy_path
    path = shared_policy_path(manager)
    policy = load_shared_daily_budget_policy(path)
    return all(policy[k] == desired['research_budget'][k] for k in
               ('max_daily_cost_usd', 'max_daily_paid_calls', 'max_alphaengine_calls_24h'))


def _sync_shared(manager, desired):
    if desired['research_budget'] is not None:
        from .host_source_budget_sync import shared_policy_path
        shared_policy_path(manager, persist=True)
    if _shared_landed(manager, desired):
        return
    from .workspace_manager import _config
    from .shared_daily_budget import load_shared_daily_budget_policy, policy_wire, ZERO_COUNTS_SCHEMA_VERSION
    from .host_source_budget_sync import shared_policy_path
    path = shared_policy_path(manager, persist=True)
    prior = load_shared_daily_budget_policy(path)
    budget = desired['research_budget']
    wire = policy_wire(**{k: budget[k] for k in ('max_daily_cost_usd','max_daily_paid_calls','max_alphaengine_calls_24h')},
                       revision=prior['revision'] + 1, prior_hash=prior['content_hash'],
                       updated_at=desired['updated_at'], actor_ref=desired['actor_ref'],
                       schema_version=ZERO_COUNTS_SCHEMA_VERSION)
    settings._write(path.parent / 'shared-daily-budget-versions' / (wire['content_hash'] + '.json'), wire)
    settings._write(path, wire)


def pending(environment, manager, desired):
    problems = []
    bindings = _consumer_policy_bindings(environment)
    for params in selections(desired):
        if not selection_landed(environment, params, bindings):
            problems.append('model')
            break
    budget = desired['research_budget']
    mission = settings.current_mission(environment.state_dir) if budget is not None else None
    # A workspace awaiting its first goal receives the durable creation defaults
    # at draft/publication, rather than an invented mission here.
    if budget is not None and mission is not None and not budget_landed(mission, budget):
        problems.append('research_budget')
    if not _shared_landed(manager, desired):
        problems.append('shared_daily_budget')
    from .host_source_budget_sync import pending as source_pending
    if desired['research_budget'] is not None and source_pending(environment.state_dir, manager, desired):
        problems.append('source_budget')
    return problems


def status(manager):
    desired = settings.load(manager) if manager else None
    if desired is None:
        return {'scope': 'all_workspaces', 'desired_hash': None, 'status': 'not_initialized',
                'pending': [], 'note': '保存后将应用于全部研究项目和新建项目。'}
    rows = []
    for environment, lifecycle in environments(manager):
        try:
            reasons = pending(environment, manager, desired)
        except (OSError, ValueError, RuntimeError, sqlite3.Error) as exc:
            reasons = ['read_unavailable']
        if reasons:
            rows.append({'environment_id': environment.environment_id, 'name': environment.name,
                         'lifecycle': lifecycle, 'reasons': reasons})
    return {'scope': 'all_workspaces', 'desired_hash': desired['content_hash'], 'revision': desired['revision'],
            'status': 'pending' if rows else 'applied', 'pending': rows,
            'material_acquisition': (desired['research_budget'] or desired['creation_budget_defaults']['budget'] or {}).get('material_acquisition'),
            'note': (f'已保存共用设置，{len(rows)}个项目将在恢复或下一次调度前应用。' if rows else '全部研究项目已采用共用设置，新建项目也将继承。')}


def reconcile_writer(server):
    """Only the single writer calls this, before it launches a new task."""
    from .workspace_host_scheme import resolve_manager_config
    from .model_routing_sync import environment_for_state_dir
    manager = resolve_manager_config(server.state_dir)
    if manager is None or settings.load(manager) is None:
        return {'status': 'unmanaged'}
    path = settings.location(manager)
    lock = path.with_suffix('.lock')
    if lock.is_symlink():
        raise settings.HostSettingsError('共用设置锁无效')
    with lock.open('a+') as stream:
        os.chmod(lock, 0o600); fcntl.flock(stream.fileno(), fcntl.LOCK_EX)
        desired = settings.load(manager)
        environment = environment_for_state_dir([item[0] for item in environments(manager)], server.state_dir)
        if environment is None:
            raise settings.HostSettingsError('当前研究项目不属于共用设置的主机')
        _sync_shared(manager, desired)
        bindings = _consumer_policy_bindings(environment)
        for params in selections(desired):
            if not selection_landed(environment, params, bindings):
                server._op_set_model_selection({**params, 'actor_ref': desired['actor_ref']})
                bindings = _consumer_policy_bindings(environment)
        budget = desired['research_budget']
        row = server.store.connection.execute('SELECT mission_ref FROM coverage_mission_pointer ORDER BY updated_at DESC LIMIT 1').fetchone()
        if budget is not None and row is not None:
            mission = server.coverage_mission.active_mission(row['mission_ref'])
            if not budget_landed(mission, budget):
                server._op_set_research_budget_authority_chain({'mission_ref': mission['mission_ref'],
                    'expected_mission_hash': mission['content_hash'], 'budget': settings.mission_budget(budget), 'actor_ref': desired['actor_ref']})
        from .host_source_budget_sync import apply as sync_source_budget
        sync_source_budget(server, manager, desired)
        from .shared_daily_budget import install_binding
        from .workspace_manager import _config
        policy_path = _config(Path(manager)).get('shared_daily_budget_policy_path')
        if policy_path:
            install_binding(server.state_dir, environment_id=environment.environment_id,
                            manager_config_path=manager, policy_path=policy_path)
        if pending(environment, manager, desired):
            raise settings.HostSettingsError('共用设置尚未完整应用，暂停新任务调度')
        return {'status': 'applied', 'desired_hash': desired['content_hash']}


def assert_reconciled(state_dir, *, model_config=None, purpose=None):
    """Read-only pre-payment gate, also covering standalone publication workers."""
    from .workspace_host_scheme import resolve_manager_config
    from .model_routing_sync import environment_for_state_dir
    manager = resolve_manager_config(state_dir)
    if manager is None:
        return
    desired = settings.load(manager)
    if desired is None:
        return
    environment = environment_for_state_dir([row[0] for row in environments(manager)], state_dir)
    if environment is None or pending(environment, manager, desired):
        raise settings.HostSettingsError('共用模型或预算设置尚待应用，新调用暂缓')
    if model_config is not None and purpose is not None:
        from .model_router import ModelRouter
        from .model_fallback_chain import tier_for
        with ModelRouter(model_config['model_router_db'], read_only=True) as router:
            pinned = router.get_policy(model_config['routing_policy_ref'])
        own = desired['selections']['purposes'].get(purpose)
        checks = ([{'purpose': purpose, **own}] if own is not None else [])
        if own is None or own['mode'] == 'tier':
            tier = tier_for(purpose)
            entry = desired['selections']['tiers'].get(tier)
            if entry is not None:
                checks.append({'tier': tier, **entry})
        thinking = desired['selections']['thinking'].get(purpose)
        if thinking is not None:
            checks.append({'purpose': purpose, 'thinking_level': thinking['level'], 'reason': thinking['reason']})
        if not all(policy_matches(pinned, item) for item in checks):
            raise settings.HostSettingsError('这次调用仍持有旧模型设置，请重新载入共用设置后继续')


def fan_out(manager, desired):
    """Saved intent survives a stopped writer; failures remain visible/pending."""
    from .governance_cli import ephemeral_call
    from .writer_protocol import RemoteError
    rows = {'synced': [], 'skipped': [], 'failed': []}
    for environment, lifecycle in environments(manager):
        entry = {'environment_id': environment.environment_id, 'name': environment.name}
        try:
            if not pending(environment, manager, desired):
                rows['skipped'].append(entry); continue
            if lifecycle != 'active' or not environment.is_running:
                rows['failed'].append({**entry, 'reason': '项目暂停或写入服务未运行，恢复时应用。'}); continue
            # A normal selection operation enters the writer's reconciliation
            # gate. Its own remaining requested operation is then idempotent.
            first = next(selections(desired), None)
            if first is None:
                raise settings.HostSettingsError('共用设置没有模型配置')
            ephemeral_call(token_config=environment.token_config, socket_path=environment.writer_socket,
                operation='set_model_selection', params=first, actor_ref=desired['actor_ref'])
            if pending(environment, manager, desired):
                raise settings.HostSettingsError('设置仍待应用')
            rows['synced'].append(entry)
        except (OSError, ValueError, RuntimeError, RemoteError, sqlite3.Error) as exc:
            rows['failed'].append({**entry, 'reason': '写入服务尚未应用共用设置，恢复后重试。'})
    view = status(manager)
    return {'status': 'pending' if rows['failed'] else 'applied', **rows,
            'environment_count': sum(len(v) for v in rows.values()), 'note': view['note'], 'host_settings': view}



def resolved_model_config(state_dir, current, purpose):
    from .workspace_host_scheme import resolve_manager_config
    from .model_routing_sync import environment_for_state_dir
    manager = resolve_manager_config(state_dir)
    if manager is None or settings.load(manager) is None:
        return current
    assert_reconciled(state_dir)
    from .model_selection import model_configs
    from .model_router import ModelRouter
    from .planner_paid_output_recovery import _authority_connection
    from .store import content_hash
    with _authority_connection(current['model_router_db']) as connection:
        router = object.__new__(ModelRouter); router.connection = connection
        pinned = router.get_policy(current['routing_policy_ref'])
    if pinned.get('content_hash') != content_hash({k:v for k,v in pinned.items() if k!='content_hash'}):
        raise settings.HostSettingsError('原模型策略内容校验失败')
    environment = environment_for_state_dir([row[0] for row in environments(manager)], state_dir)
    heads = {p['policy_version_ref']:p for p in consumer_policies(environment) if p['id']==pinned['id']}
    # Different purposes can legitimately consume different versions of one
    # family. An exact active pin is unambiguous; do not collapse them into a
    # supposed single family head. A stale pin still needs one unique head.
    policy = heads.get(pinned['policy_version_ref'])
    if policy is not None:
        if policy['content_hash'] != pinned['content_hash']:
            raise settings.HostSettingsError('原模型策略内容校验失败')
    elif len(heads) == 1:
        policy = next(iter(heads.values()))
    else:
        raise settings.HostSettingsError('当前模型策略仍未完整同步，新调用暂缓')
    resolved = dict(current)
    resolved['routing_policy_ref'] = policy['policy_version_ref']
    desired = settings.load(manager)
    configs = [pin['config'] for pin in model_configs(state_dir)]
    same_router = [config for config in configs if Path(config.get('model_router_db','')).resolve()==Path(current['model_router_db']).resolve()]
    slots = list(current.get('credential_slot_refs') or [])
    for config in same_router:
        slots.extend(config.get('credential_slot_refs') or [])
    resolved['credential_slot_refs'] = list(dict.fromkeys(slots))
    if desired['research_budget'] is not None:
        refs = {config['budget_policy_ref'] for config in configs
                if Path(config.get('budget_db','')).resolve()==Path(current['budget_db']).resolve()}
        if len(refs)!=1:
            raise settings.HostSettingsError('当前研究预算授权尚未同步，新调用暂缓')
        resolved['budget_policy_ref'] = next(iter(refs))
    assert_reconciled(state_dir, model_config=resolved, purpose=purpose)
    return resolved
