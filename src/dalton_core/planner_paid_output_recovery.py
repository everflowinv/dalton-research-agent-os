"""One normal-budget successor for a strongly proved paid planner overrun.

The hold is an immutable certificate written only after settlement. Recovery
also replays its Work, formal result, route, profile and settlement authorities;
neither a failure string nor an unverified hold can admit another paid call.
"""
from __future__ import annotations

import json
import re
from contextlib import closing, contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

from .contracts import WorkOrder, ResultEnvelope
from .model_output_contract_hold import TABLE, _MESSAGES, HOLD_SECONDS, scope
from .store import canonical_json, content_hash

MARKER = ":paid-output-recovery:"


@contextmanager
def _authority_connection(path):
    """Use existing live sidecars or the already proved clean cold reader."""
    import sqlite3
    from .readonly_sqlite import connect_read_only, connect_cold_wal_snapshot
    target = Path(path).absolute()
    with target.open('rb') as stream:
        header = stream.read(20)
    cold = header[18:20] == b'\x02\x02' and not any(Path(str(target)+s).exists() for s in ('-wal','-shm'))
    manager = connect_cold_wal_snapshot(target) if cold else closing(connect_read_only(target))
    with manager as connection:
        connection.row_factory = sqlite3.Row
        yield connection


@contextmanager
def recovery_readers(router_path, budget_path, *, now):
    from .model_router import ModelRouter
    from .thesis_impact_budget import ThesisImpactBudgetStore
    with _authority_connection(router_path) as rc:
        router = ModelRouter.__new__(ModelRouter)
        router.connection, router.clock = rc, lambda:now
        if not Path(budget_path).is_file():
            yield router, None
        else:
            with _authority_connection(budget_path) as bc:
                budget = ThesisImpactBudgetStore.__new__(ThesisImpactBudgetStore)
                budget.connection = bc
                yield router, budget


def _router_record(router, table, ref_column, json_column, hash_column, ref):
    row = router.connection.execute('SELECT * FROM '+table+' WHERE '+ref_column+'=?', (ref,)).fetchone()
    if row is None:
        raise ValueError('paid planner recovery router record absent')
    wire = json.loads(row[json_column])
    if (canonical_json(wire) != row[json_column] or wire['content_hash'] != row[hash_column]
            or content_hash({k:v for k,v in wire.items() if k!='content_hash'}) != row[hash_column]):
        raise ValueError('paid planner recovery router authority drifted')
    return wire


def recovery_for_work(connection: Any, router: Any, budget_store: Any, *,
                      work_ref: str, mission_version_ref: str, policy_ref: str,
                      expected_budget: Mapping[str, Any], now: datetime,
                      require_active: bool = True,
                      allow_successor_proof: bool = False) -> dict[str, Any] | None:
    if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='scheduler_formal_results'").fetchone() is None:
        return None
    work_row = connection.execute("SELECT * FROM scheduler_work_orders WHERE work_order_id=?", (work_ref,)).fetchone()
    formal = connection.execute("SELECT * FROM scheduler_formal_results WHERE work_order_id=?", (work_ref,)).fetchone()
    if work_row is None or formal is None or formal['terminal_state'] != 'failed':
        return None
    work_wire = json.loads(work_row['work_order_json'])
    work = WorkOrder.from_dict(work_wire)
    meta = work.metadata
    if (meta.get('control_plane') != 'cockpit' or meta.get('purpose') != 'plan'
            or meta.get('mission_version_ref') != mission_version_ref
            or (MARKER in str(meta.get('request_id', '')) and not allow_successor_proof)
            or dict(work.budget) != dict(expected_budget)):
        return None
    if (work.id != work_ref or canonical_json(work_wire) != work_row['work_order_json']
            or content_hash(work_wire) != work_row['work_order_hash']):
        raise ValueError('paid planner recovery Work authority drifted')
    result_wire = json.loads(formal['result_envelope_json'])
    result = ResultEnvelope.from_dict(result_wire)
    error = result.error or {}
    if (result.status != 'failed' or error.get('code') != 'PROVIDER_BUDGET_EXCEEDED'
            or error.get('source') != 'openclaw-model-adapter'
            or error.get('message') not in _MESSAGES):
        return None
    formal_wire = {key: formal[column] for key, column in (
        ('id', 'result_record_id'), ('work_order_id', 'work_order_id'),
        ('attempt_number', 'attempt_number'), ('result_envelope_id', 'result_envelope_id'),
        ('result_envelope_hash', 'result_envelope_hash'), ('terminal_state', 'terminal_state'),
        ('created_at', 'created_at'))}
    if (canonical_json(result_wire) != formal['result_envelope_json']
            or result.work_order_ref != work_ref or result.id != formal['result_envelope_id']
            or content_hash(result_wire) != formal['result_envelope_hash']
            or content_hash(formal_wire) != formal['content_hash']):
        raise ValueError('paid planner recovery formal result drifted')
    if router.connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (TABLE,)).fetchone() is None:
        return None
    route_ref = result.metadata.get('route_decision_ref')
    if not isinstance(route_ref, str):
        return None
    route = _router_record(router, 'model_route_decisions','decision_id','decision_json','decision_hash',route_ref)
    profile = _router_record(router, 'model_endpoint_profile_versions','profile_version_ref','profile_json','profile_hash',route['selected_profile_version_ref'])
    current_policy = _router_record(router, 'model_routing_policy_versions','policy_version_ref','policy_json','policy_hash',policy_ref)
    policy = _router_record(router, 'model_routing_policy_versions','policy_version_ref','policy_json','policy_hash',route['policy_version_ref'])
    same_policy = current_policy['content_hash'] == policy['content_hash']
    wanted = scope(profile, policy_ref=route['policy_version_ref'], policy_hash=policy['content_hash'],
                   purpose='plan', budget=work.budget, thinking_level=route['thinking_level'])
    row = router.connection.execute('SELECT * FROM '+TABLE+' WHERE scope_hash=? AND route_ref=? ORDER BY until DESC LIMIT 1',
                                    (content_hash(wanted), route_ref)).fetchone()
    if row is None:
        return None
    hold = json.loads(row['record_json'])
    observed = datetime.fromisoformat(hold['observed_at'])
    until = datetime.fromisoformat(hold['until'])
    if (canonical_json(hold) != row['record_json'] or hold['scope'] != wanted
            or hold['scope_hash'] != row['scope_hash'] or hold['hold_id'] != row['hold_id']
            or hold['until'] != row['until'] or hold['observed_at'] != row['observed_at']
            or hold['content_hash'] != row['content_hash']
            or content_hash({k:v for k,v in hold.items() if k!='content_hash'}) != row['content_hash']
            or (until-observed).total_seconds() != HOLD_SECONDS):
        raise ValueError('paid planner recovery hold authority drifted')
    if now < observed or (require_active and (now >= until or not same_policy)):
        return None
    proof = hold['proof']
    if budget_store is None:
        return None
    paid = budget_store.admission(work_order_ref=work_ref, attempt_number=route['attempt_number'], phase='assessment')
    if paid is None or paid['settlement'] is None:
        return None
    admission, settlement = paid['admission'], paid['settlement']
    binding = paid.get('mission_binding')
    if binding is None or binding.get('mission_version_ref') != mission_version_ref:
        return None
    expected = {
        'work_ref': work_ref, 'work_hash': work_row['work_order_hash'],
        'route_ref': route_ref, 'route_hash': route['content_hash'],
        'result_ref': result.id, 'result_hash': formal['result_envelope_hash'],
        'invocation_ref': result.invocation_ref,
        'broker_response_hash': result.metadata.get('broker_response_hash'),
        'broker_version': result.metadata.get('broker_version'),
        'runtime_version': result.metadata.get('runtime_version'),
        'runtime_ref': profile['adapter_ref'],
        'admission_ref': admission['admission_id'], 'admission_hash': admission['content_hash'],
        'settlement_ref': settlement['settlement_id'], 'settlement_hash': settlement['content_hash'],
    }
    if (any(proof.get(k) != value for k, value in expected.items())
            or route['work_order_ref'] != work_ref or route['work_order_hash'] != work_row['work_order_hash']
            or route['policy_hash'] != policy['content_hash']
            or route['attempt_number'] != formal['attempt_number']
            or route['outcome'] != 'selected'
            or result.metadata.get('profile_version_ref') != profile['profile_version_ref']
            or result.metadata.get('thinking_level') != route['thinking_level']
            or result.metadata.get('dispatch_proof') is not None
            or type(proof.get('reported_output_tokens')) is not int
            or proof['reported_output_tokens'] <= work.budget['max_output_tokens']
            or type(proof.get('actual_cost_micros')) is not int or proof['actual_cost_micros'] < 0
            or settlement['actual_micros'] < proof['actual_cost_micros']
            or datetime.fromisoformat(result.created_at) != datetime.fromisoformat(hold['observed_at'])):
        raise ValueError('paid planner recovery source/settlement binding drifted')
    suffix = MARKER + content_hash({'parent_work_ref': work_ref, 'parent_work_hash': work_row['work_order_hash'],
                                    'formal_hash': formal['result_envelope_hash'], 'hold_ref': hold['hold_id'],
                                    'hold_hash': hold['content_hash']})[:32]
    return {'parent_work_ref': work_ref, 'parent_work_hash': work_row['work_order_hash'],
            'formal_hash': formal['result_envelope_hash'], 'hold_ref': hold['hold_id'],
            'hold_hash': hold['content_hash'], 'until': hold['until'],
            'active': same_policy and now < datetime.fromisoformat(hold['until']), 'suffix': suffix,
            'parent_request_id': meta['request_id']}


def successor_work(parent: WorkOrder, proof: Mapping[str, Any]) -> WorkOrder:
    """Reconstruct the original identity before deriving its one successor."""
    from .cockpit_model import build_work
    from .call_budget import budget_fingerprint
    b, m = parent.budget, parent.metadata
    kwargs = dict(purpose='plan', request_id=m['request_id'], prompt=parent.question,
                  mission_version_ref=m['mission_version_ref'], created_at=parent.created_at,
                  max_input_tokens=b['max_input_tokens'], max_output_tokens=b['max_output_tokens'],
                  max_cost_usd=b['max_cost_usd'], max_seconds=b['max_seconds'],
                  provider_retry=m.get('provider_retry'), broker_frame_policy=m.get('broker_frame_policy'))
    fingerprint = budget_fingerprint({'max_input_tokens':b['max_input_tokens'],
        'max_output_tokens':b['max_output_tokens'], 'max_cost_usd':b['max_cost_usd'],
        'timeout_seconds':b['max_seconds']})
    for identity in (None, fingerprint):
        old = build_work(**kwargs, budget_identity=identity)
        if old.to_dict() == parent.to_dict():
            return build_work(**{**kwargs, 'request_id':m['request_id']+proof['suffix']},
                              budget_identity=identity)
    raise ValueError('paid planner recovery cannot reconstruct exact original Work identity')


def _successor_status(connection: Any, child: WorkOrder) -> str | None:
    row = connection.execute('SELECT work_order_json,work_order_hash FROM scheduler_work_orders WHERE work_order_id=?', (child.id,)).fetchone()
    if row is None:
        return None
    if (row['work_order_json'] != canonical_json(child.to_dict())
            or row['work_order_hash'] != content_hash(child.to_dict())):
        raise ValueError('paid planner recovery successor content drifted')
    formal = connection.execute('SELECT terminal_state FROM scheduler_formal_results WHERE work_order_id=?', (child.id,)).fetchone()
    return 'pending' if formal is None else formal[0]


def paid_successor_failure(connection, router, budget, *, work_ref, mission_version_ref,
                           policy_ref, expected_budget, now):
    """The suffix alone is not proof that a second provider executed/billed."""
    return recovery_for_work(connection, router, budget, work_ref=work_ref,
        mission_version_ref=mission_version_ref, policy_ref=policy_ref,
        expected_budget=expected_budget, now=now, require_active=False,
        allow_successor_proof=True) is not None


def planner_candidate(*, state_dir: Path, config_path: Path, mission: Mapping[str, Any],
                      state_hash: str, now: datetime, scheduler_db: Path | None = None,
                      include_payload: bool = False) -> dict[str, Any] | None:
    """Bounded RO probe used by both dispatch gates; no model is dispatched."""
    if not re.fullmatch('[a-f0-9]{64}', state_hash):
        return None
    from .readonly_sqlite import connect_read_only
    from .model_router import ModelRouter
    from .thesis_impact_budget import ThesisImpactBudgetStore
    from .cockpit_model import CockpitModel
    from .research_planner_cli import (effective_planner_model_config, MAX_INPUT_TOKENS,
        MAX_OUTPUT_TOKENS, MAX_COST_USD, TIMEOUT_SECONDS)
    config = effective_planner_model_config(state_dir=state_dir, model_config_path=config_path)
    model = CockpitModel(config, scheduler_db=str(scheduler_db or state_dir/'scheduler.sqlite'),
                         max_input_tokens=MAX_INPUT_TOKENS, max_output_tokens=MAX_OUTPUT_TOKENS,
                         max_cost_usd=MAX_COST_USD, timeout_seconds=TIMEOUT_SECONDS)
    effective = model.budget_for('plan')
    expected = {'max_input_tokens':effective['max_input_tokens'], 'max_output_tokens':effective['max_output_tokens'],
                'max_total_tokens':effective['max_input_tokens']+effective['max_output_tokens'],
                'max_cost_usd':effective['max_cost_usd'], 'max_seconds':effective['timeout_seconds']}
    with _authority_connection(scheduler_db or state_dir/'scheduler.sqlite') as connection, \
            recovery_readers(config['model_router_db'], config['budget_db'], now=now) as (router, budget):
        import sqlite3
        connection.row_factory = sqlite3.Row
        if router.connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (TABLE,)).fetchone() is None:
            return None
        # Holds are a small, indexed one-hour set, not a Scheduler history scan.
        rows = router.connection.execute('SELECT record_json FROM '+TABLE+' ORDER BY until DESC LIMIT 32').fetchall()
        for row in rows:
            hold = json.loads(row[0])
            if hold['scope'].get('purpose') != 'plan':
                continue
            work_ref = hold['proof']['work_ref']
            parent_row = connection.execute('SELECT work_order_json FROM scheduler_work_orders WHERE work_order_id=?', (work_ref,)).fetchone()
            if parent_row is None:
                continue
            parent = WorkOrder.from_dict(json.loads(parent_row[0]))
            request = parent.metadata.get('request_id', '')
            if request != state_hash[:32] and not request.startswith(state_hash[:32]+':'):
                continue
            try:
                frozen = json.loads(parent.question.rsplit('\nRESEARCH_STATE=',1)[1])
            except (IndexError, ValueError, TypeError):
                continue
            if (frozen.get('content_hash') != state_hash
                    or (frozen.get('prompt_projection') or {}).get('full_state_hash',state_hash) != state_hash):
                continue
            proof = recovery_for_work(connection, router, budget, work_ref=work_ref,
                mission_version_ref=mission['id'], policy_ref=config['routing_policy_ref'],
                expected_budget=expected, now=now, require_active=False)
            if proof is not None:
                child = successor_work(parent, proof)
                status = _successor_status(connection, child)
                if not proof['active'] and status is None:
                    continue  # Expired/config-changed hold cannot create new work.
                if status == 'failed' and paid_successor_failure(connection, router, budget,
                        work_ref=child.id,mission_version_ref=mission['id'],
                        policy_ref=config['routing_policy_ref'],expected_budget=expected,now=now):
                    return None  # One generation; another failure stays explicit.
                return {**proof, 'successor_work_ref':child.id, 'successor_status':status,
                        **({'_parent_prompt':parent.question} if include_payload else {})}
    return None


def proved_paid_successor(*, scheduler_db, config, mission, recovery, now):
    with _authority_connection(scheduler_db) as connection, \
            recovery_readers(config['model_router_db'], config['budget_db'], now=now) as (router,budget):
        row=connection.execute('SELECT work_order_json FROM scheduler_work_orders WHERE work_order_id=?',
                               (recovery['successor_work_ref'],)).fetchone()
        if row is None:
            return False
        work=WorkOrder.from_dict(json.loads(row[0]))
        return paid_successor_failure(connection,router,budget,work_ref=work.id,
            mission_version_ref=mission['id'],policy_ref=config['routing_policy_ref'],
            expected_budget=work.budget,now=now)
