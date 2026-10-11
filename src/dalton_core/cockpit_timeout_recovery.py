"""Finite normal-budget recovery; TIMEOUT never proves no-send or releases fees."""
from __future__ import annotations
import json
import re
from datetime import datetime, timedelta
from typing import Mapping
from .contracts import WorkOrder, ResultEnvelope
from .store import canonical_json, content_hash
from .provider_retry import validate_provider_retry
VERSION = 'cockpit-timeout-maintenance:0.1'
MARKER = ':timeout-maintenance:'
TABLE = 'cockpit_timeout_maintenance_authorizations'
PURPOSES = frozenset({'initial_screen', 'deep_insight_gate'})
MAINTENANCE_WINDOW_SECONDS = 86400
MAINTENANCE_BACKOFF_SECONDS = 60


def _time(value):
    at = datetime.fromisoformat(value)
    if at.tzinfo is None:
        raise ValueError('timeout authority timestamp has no timezone')
    return at


def failure_proof(connection, router, budget, *, work_ref, mission):
    """Read exact authorities, including all reservations, without constructors."""
    from .scheduler import Scheduler
    reader = Scheduler.__new__(Scheduler)
    reader.connection = connection
    authority = reader.work_order_authority(work_ref)
    formal = reader.formal_result(work_ref)
    if authority is None or formal is None or formal['terminal_state'] != 'failed':
        return None
    work = WorkOrder.from_dict(authority['work_order'])
    meta = work.metadata
    if (meta.get('control_plane') != 'cockpit' or meta.get('purpose') not in PURPOSES
            or meta.get('timeout_recovery') is not None or MARKER in meta.get('request_id', '')
            or meta.get('mission_version_ref') != mission['id']
            or meta.get('mission_version_hash') not in (None, mission['content_hash'])):
        return None
    wire = formal['result_envelope']
    result = ResultEnvelope.from_dict(wire)
    formal_body = {k: formal[c] for k, c in (
        ('id','result_record_id'), ('work_order_id','work_order_id'),
        ('attempt_number','attempt_number'), ('result_envelope_id','result_envelope_id'),
        ('result_envelope_hash','result_envelope_hash'), ('terminal_state','terminal_state'),
        ('created_at','created_at'))}
    if (content_hash(formal_body) != formal['content_hash']
            or content_hash(wire) != formal['result_envelope_hash']
            or result.id != formal['result_envelope_id']
            or result.work_order_ref != work_ref or result.status != 'failed'):
        raise ValueError('timeout formal authority drifted')
    error, md = result.error or {}, result.metadata
    failures = md.get('chain_failures')
    if (error.get('code') != 'MODEL_CHAIN_EXHAUSTED'
            or md.get('control_plane_failure') is not True or md.get('dispatch_state') != 'unknown'
            or not isinstance(failures, list) or not failures
            or any(not isinstance(f, Mapping) or f.get('code') != 'TIMEOUT'
                   or f.get('failure_class') != 'unclassified_failure' for f in failures)):
        return None
    route_ref = md.get('route_decision_ref')
    if not isinstance(route_ref, str) or budget is None:
        return None
    route = router.get_decision(route_ref)
    if (route['outcome'] != 'selected' or route['work_order_ref'] != work_ref
            or route['work_order_hash'] != authority['work_order_hash']
            or route['attempt_number'] != formal['attempt_number']):
        raise ValueError('timeout route authority drifted')
    rows = budget.connection.execute(
        'SELECT attempt_number,phase FROM thesis_impact_day_admissions '
        'WHERE work_order_ref=? ORDER BY attempt_number,phase', (work_ref,)).fetchall()
    if not rows:
        return None
    costs, selected = [], False
    for row in rows:
        paid = budget.admission(work_order_ref=work_ref,
                               attempt_number=row['attempt_number'], phase=row['phase'])
        admission, settlement, binding = paid['admission'], paid['settlement'], paid['mission_binding'] or {}
        admitted_route = router.get_decision(admission['route_decision_ref'])
        if (binding.get('mission_version_ref') != mission['id']
                or binding.get('mission_version_hash') != mission['content_hash']
                or admitted_route['work_order_ref'] != work_ref
                or admitted_route['work_order_hash'] != authority['work_order_hash']
                or admitted_route['attempt_number'] != row['attempt_number']
                or admission.get('purpose') != meta['purpose']):
            raise ValueError('timeout fee authority drifted')
        selected |= row['attempt_number'] == formal['attempt_number']
        # Unknown fees retain at least the full original reservation. This is
        # a conservative proof bound, never a rewrite/second ledger charge.
        costs.append({'admission_ref':admission['admission_id'],
                      'admission_hash':admission['content_hash'],
                      'reserved_micros':admission['reserved_micros'],
                      'accounted_micros':max(admission['reserved_micros'],
                                            0 if settlement is None else settlement['actual_micros'])})
    if not selected:
        return None
    request = str(meta['request_id'])
    return {'parent_work_ref':work.id, 'parent_work_hash':authority['work_order_hash'],
            'formal_ref':formal['result_record_id'], 'formal_hash':formal['content_hash'],
            'result_ref':result.id, 'result_hash':formal['result_envelope_hash'],
            'route_ref':route_ref, 'route_hash':route['content_hash'],
            'mission_version_ref':mission['id'], 'mission_version_hash':mission['content_hash'],
            'purpose':meta['purpose'], 'semantic_hash':semantic_hash(work),
            'parent_request_id':request, 'failed_at':formal['created_at'],
            'provider_retry':meta.get('provider_retry'), 'costs':costs,
            'accounted_micros':sum(item['accounted_micros'] for item in costs)}


def _row(connection, semantic_hash):
    if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (TABLE,)).fetchone() is None:
        return None
    row = connection.execute('SELECT * FROM '+TABLE+' WHERE semantic_hash=?', (semantic_hash,)).fetchone()
    if row is None:
        return None
    record = json.loads(row['record_json'])
    if (canonical_json(record) != row['record_json'] or record['content_hash'] != row['content_hash']
            or content_hash({k:v for k,v in record.items() if k!='content_hash'}) != row['content_hash']
            or record['semantic_hash'] != semantic_hash or record['version'] != VERSION
            or record['id'] != row['authorization_id']):
        raise ValueError('timeout maintenance authorization drifted')
    return record


def reserve(scheduler, router, budget, *, work, mission, config, now):
    proof = failure_proof(scheduler.connection, router, budget, work_ref=work.id, mission=mission)
    if proof is None or content_hash(work.to_dict()) != proof['parent_work_hash']:
        return None
    previous = _row(scheduler.connection, proof['semantic_hash'])
    if previous is not None:
        verify(scheduler.connection, router, budget, previous, mission=mission)
        return previous if previous['proof']['parent_work_ref'] == work.id else None
    policy = proof.get('provider_retry')
    original = None if policy is None else validate_provider_retry(policy).get('unknown_recovery')
    if original is not None and original['max_fresh_work_orders'] == 0:
        return None
    seconds = MAINTENANCE_WINDOW_SECONDS if original is None else original['max_elapsed_seconds']
    backoff = MAINTENANCE_BACKOFF_SECONDS if original is None else original['retry_backoff_seconds']
    failed_at = _time(proof['failed_at'])
    start = failed_at if original is not None else now
    if now < failed_at + timedelta(seconds=backoff) or now >= start + timedelta(seconds=seconds):
        return None
    record = {'version':VERSION, 'actor':'automation:cockpit-timeout-maintenance',
              'semantic_hash':proof['semantic_hash'], 'proof':proof,
              'policy_source':'versioned_maintenance' if original is None else 'frozen_unknown_recovery',
              'maximum_successors':1, 'config_hash':content_hash(dict(config)),
              'created_at':now.isoformat(),
              'deadline':(start+timedelta(seconds=seconds)).isoformat()}
    record['id'] = 'cockpit-timeout-authorization:'+content_hash(record)[:32]
    record['suffix'] = MARKER+content_hash({'version':VERSION,'semantic':proof['semantic_hash']})[:32]
    record['content_hash'] = content_hash(record)
    with scheduler._transaction() as cur:
        cur.execute('CREATE TABLE IF NOT EXISTS '+TABLE+' ('
                    'authorization_id TEXT PRIMARY KEY, semantic_hash TEXT UNIQUE NOT NULL, '
                    'record_json TEXT NOT NULL, content_hash TEXT NOT NULL)')
        for op in ('UPDATE','DELETE'):
            cur.execute('CREATE TRIGGER IF NOT EXISTS '+TABLE+'_'+op.lower()+' BEFORE '+op+' ON '+TABLE+
                        " BEGIN SELECT RAISE(ABORT,'timeout authorizations are append-only'); END")
        cur.execute('CREATE TRIGGER IF NOT EXISTS '+TABLE+'_insert BEFORE INSERT ON '+TABLE+
                    " WHEN dalton_scheduler_authorized()=0 BEGIN SELECT RAISE(ABORT,'Scheduler authority required'); END")
        cur.execute('INSERT OR IGNORE INTO '+TABLE+' VALUES(?,?,?,?)',
                    (record['id'],record['semantic_hash'],canonical_json(record),record['content_hash']))
    saved = _row(scheduler.connection, proof['semantic_hash'])
    verify(scheduler.connection, router, budget, saved, mission=mission)
    return saved if saved['proof']['parent_work_ref'] == work.id else None


def verify(connection, router, budget, record, *, mission):
    saved = _row(connection, record['semantic_hash'])
    if saved != dict(record):
        raise ValueError('timeout maintenance binding is not authorized')
    current = failure_proof(connection, router, budget,
                            work_ref=record['proof']['parent_work_ref'], mission=mission)
    if current != record['proof']:
        raise ValueError('timeout maintenance parent/fee authority changed')
    return record


CONSUMERS = 'cockpit_timeout_maintenance_consumers'

def consume(scheduler, record, work, *, now):
    """Only one physical successor may consume an authorization, across epochs."""
    wire = work.to_dict()
    existing = scheduler.formal_result(work.id)
    if existing is None and now >= _time(record['deadline']):
        raise ValueError('timeout maintenance authorization expired before completion')
    with scheduler._transaction() as cur:
        cur.execute('CREATE TABLE IF NOT EXISTS '+CONSUMERS+' ('
                    'authorization_id TEXT PRIMARY KEY, work_ref TEXT UNIQUE NOT NULL, work_hash TEXT NOT NULL)')
        for op in ('UPDATE','DELETE'):
            cur.execute('CREATE TRIGGER IF NOT EXISTS '+CONSUMERS+'_'+op.lower()+' BEFORE '+op+' ON '+CONSUMERS+
                        " BEGIN SELECT RAISE(ABORT,'timeout consumers are append-only'); END")
        cur.execute('CREATE TRIGGER IF NOT EXISTS '+CONSUMERS+'_insert BEFORE INSERT ON '+CONSUMERS+
                    " WHEN dalton_scheduler_authorized()=0 BEGIN SELECT RAISE(ABORT,'Scheduler authority required'); END")
        cur.execute('INSERT OR IGNORE INTO '+CONSUMERS+' VALUES(?,?,?)',
                    (record['id'],work.id,content_hash(wire)))
        row = cur.execute('SELECT * FROM '+CONSUMERS+' WHERE authorization_id=?', (record['id'],)).fetchone()
        if row['work_ref'] != work.id or row['work_hash'] != content_hash(wire):
            raise ValueError('timeout maintenance authorization was already consumed by different Work')


def semantic_hash(work):
    request = str(work.metadata['request_id'])
    semantic = re.sub(r':(?:capacity-policy|transport-policy):[0-9a-f]{16}', '', request)
    semantic = re.sub(r':(?:route-admission:[0-9a-f]{64}|capacity-recovery:\d+:[0-9a-f]{16})$', '', semantic)
    return content_hash({'mission':work.metadata['mission_version_ref'],
                         'purpose':work.metadata['purpose'], 'request':semantic,
                         'prompt_hash':content_hash(work.question)})


def prior_authorization(connection, work):
    return _row(connection, semantic_hash(work))


def saved_successor(scheduler, record):
    """An already answered child stays freely readable after config changes."""
    if scheduler.connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (CONSUMERS,)).fetchone() is None:
        return None
    row = scheduler.connection.execute('SELECT * FROM '+CONSUMERS+' WHERE authorization_id=?', (record['id'],)).fetchone()
    if row is None:
        return None
    stored = scheduler.work_order_authority(row['work_ref'])
    if stored is None:
        return None
    work = WorkOrder.from_dict(stored['work_order'])
    if stored['work_order_hash'] != row['work_hash'] or work.metadata.get('timeout_recovery') != record:
        raise ValueError('timeout successor consumer authority drifted')
    formal = scheduler.formal_result(work.id)
    if formal is None or formal['terminal_state'] != 'succeeded':
        return None
    result = ResultEnvelope.from_dict(formal['result_envelope'])
    body = {k:formal[c] for k,c in (
        ('id','result_record_id'), ('work_order_id','work_order_id'),
        ('attempt_number','attempt_number'), ('result_envelope_id','result_envelope_id'),
        ('result_envelope_hash','result_envelope_hash'), ('terminal_state','terminal_state'),
        ('created_at','created_at'))}
    if (content_hash(body)!=formal['content_hash']
            or content_hash(result.to_dict())!=formal['result_envelope_hash']
            or result.work_order_ref!=work.id or result.id!=formal['result_envelope_id']
            or result.status!='succeeded'):
        raise ValueError('timeout successor formal authority drifted')
    return work, formal
