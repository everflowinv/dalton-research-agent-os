"""One-hour, exact-scope holds proved by a paid native output-budget failure.

This is not provider health or an output-budget exception. Historical Work,
telemetry and settlements remain unchanged. New routing may skip the same
profile under the same purpose/configuration, then follow the owner's chain.
"""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any, Mapping

from .store import canonical_json, content_hash

HOLD_SECONDS = 3600
SKIP_REASON = 'paid_output_contract_hold'
TABLE = 'model_output_contract_holds'
_HASH = re.compile(r'[a-f0-9]{64}')
_MESSAGES = frozenset({
    'provider max_output_tokens telemetry exceeds WorkOrder budget',
    'provider output usage exceeds requested maxTokens',
})


def _time(value: str) -> datetime:
    moment = datetime.fromisoformat(value.replace('Z', '+00:00'))
    if moment.tzinfo is None:
        raise ValueError('output-contract proof needs an aware timestamp')
    return moment.astimezone(timezone.utc)


def scope(profile: Mapping[str, Any], *, policy_ref: str, policy_hash: str,
          purpose: str | None, budget: Mapping[str, Any], thinking_level: str) -> dict[str, Any]:
    return {'profile_version_ref': profile['profile_version_ref'],
            'profile_hash': profile['content_hash'], 'provider': profile['provider'],
            'model': profile['model'], 'adapter_ref': profile['adapter_ref'],
            'policy_version_ref': policy_ref, 'policy_hash': policy_hash,
            'purpose': purpose, 'budget': dict(budget), 'thinking_level': thinking_level}


def active(connection: Any, profile: Mapping[str, Any], *, policy_ref: str,
           policy_hash: str, purpose: str | None, budget: Mapping[str, Any],
           thinking_level: str, now: datetime) -> dict[str, Any] | None:
    # Older immutable snapshots remain readable without creating a table.
    if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (TABLE,)).fetchone() is None:
        return None
    wanted = scope(profile, policy_ref=policy_ref, policy_hash=policy_hash,
                   purpose=purpose, budget=budget, thinking_level=thinking_level)
    row = connection.execute('SELECT * FROM '+TABLE+' WHERE scope_hash=? AND '
                             'julianday(until)>julianday(?) ORDER BY until DESC LIMIT 1',
                             (content_hash(wanted), now.isoformat())).fetchone()
    if row is None:
        return None
    record = json.loads(row['record_json'])
    if (record['scope'] != wanted or record['scope_hash'] != row['scope_hash']
            or record['hold_id'] != row['hold_id'] or record['until'] != row['until']
            or record['content_hash'] != row['content_hash']
            or content_hash({k:v for k,v in record.items() if k!='content_hash'}) != row['content_hash']
            or _time(record['until']) - _time(record['observed_at']) != timedelta(seconds=HOLD_SECONDS)):
        raise ValueError('output-contract hold authority drifted')
    if now < _time(record['observed_at']):
        return None
    return record


def record(router: Any, *, work: Any, route: Mapping[str, Any], profile: Mapping[str, Any],
           invocation: Any, result: Any, budget_store: Any, admission_id: str, preview: bool = False) -> dict[str, Any] | None:
    """Consume a real settled failure, never missing/unknown/post-send evidence.

    Cost and full reported output remain on the original invocation. The
    settlement authority is reread, so a pending settlement cannot open a hold.
    """
    err = result.error or {}
    use = invocation.usage or {}
    meta = result.metadata or {}
    telemetry = use.get('raw_provider_telemetry') or {}
    cost = telemetry.get('cost') or {}
    output = use.get('output_tokens')
    if (result.status != 'failed' or err.get('code') != 'PROVIDER_BUDGET_EXCEEDED'
            or err.get('source') != 'openclaw-model-adapter'
            or err.get('message') not in _MESSAGES
            or meta.get('dispatch_proof') is not None
            or use.get('metering_source') != 'provider_reported'
            or type(output) is not int or output <= work.budget['max_output_tokens']
            or cost.get('available') is not True
            or isinstance(cost.get('usd'), bool)
            or not isinstance(cost.get('usd'), (int, float, str))
            or not isinstance(meta.get('broker_response_hash'), str)
            or not _HASH.fullmatch(meta['broker_response_hash'])
            or telemetry.get('response_content_hash') != meta['broker_response_hash']
            or not isinstance(meta.get('runtime_version'), str)
            or not isinstance(meta.get('broker_version'), str)):
        return None
    # Do not let a caller manufacture the scope from a stale/mismatched route.
    held_route = router.get_decision(route['id'])
    held_profile = router.get_profile(profile['profile_version_ref'])
    if held_route != dict(route) or held_profile != dict(profile):
        raise ValueError('output-contract route/profile proof differs')
    if (route.get('outcome') != 'selected'
            or route.get('work_order_ref') != work.id
            or route.get('work_order_hash') != content_hash(work.to_dict())
            or route.get('selected_profile_version_ref') != invocation.profile_ref
            or invocation.profile_ref != profile['profile_version_ref']
            or invocation.provider != profile['provider'] or invocation.model != profile['model']
            or invocation.model_family != profile['family']
            or invocation.runtime_ref != profile['adapter_ref']
            or invocation.actor_ref != 'runtime:openclaw-model-broker'
            or invocation.work_order_ref != work.id or result.work_order_ref != work.id
            or result.invocation_ref != invocation.id
            or meta.get('route_decision_ref') != route['id']
            or meta.get('profile_version_ref') != profile['profile_version_ref']
            or use.get('thinking_level') != route.get('thinking_level')
            or meta.get('thinking_level') != route.get('thinking_level')):
        raise ValueError('output-contract execution binding differs')
    paid = budget_store.admission(work_order_ref=work.id,
                                  attempt_number=route['attempt_number'], phase='assessment')
    if paid is None or paid['admission']['admission_id'] != admission_id:
        raise ValueError('output-contract settlement admission differs')
    admission, settlement = paid['admission'], paid['settlement']
    if settlement is None:
        return None
    micros = int((Decimal(str(cost['usd'])) * 1_000_000).quantize(Decimal('1'), rounding=ROUND_HALF_UP))
    if micros < 0 or admission['work_order_ref'] != work.id or settlement['actual_micros'] < micros:
        raise ValueError('output-contract actual cost was not fully settled')
    moment = _time(result.created_at)
    until = moment + timedelta(seconds=HOLD_SECONDS)
    now = router._now()
    if moment > now:
        raise ValueError('output-contract failure observation is in the future')
    if now >= until:
        return None
    binding = scope(profile, policy_ref=route['policy_version_ref'], policy_hash=route['policy_hash'],
                    purpose=(work.metadata or {}).get('purpose'), budget=work.budget,
                    thinking_level=route['thinking_level'])
    proof = {'work_ref': work.id, 'work_hash': content_hash(work.to_dict()),
             'route_ref': route['id'], 'route_hash': route['content_hash'],
             'invocation_ref': invocation.id, 'invocation_hash': content_hash(invocation.to_dict()),
             'result_ref': result.id, 'result_hash': content_hash(result.to_dict()),
             'broker_response_hash': meta['broker_response_hash'],
             'broker_version': meta['broker_version'], 'runtime_version': meta['runtime_version'],
             'runtime_ref': invocation.runtime_ref,
             'reported_output_tokens': output, 'actual_cost_micros': micros,
             'admission_ref': admission_id, 'admission_hash': admission['content_hash'],
             'settlement_ref': settlement['settlement_id'], 'settlement_hash': settlement['content_hash']}
    wire = {'schema_version':'model-output-contract-hold:0.1', 'scope':binding,
            'scope_hash':content_hash(binding), 'proof':proof,
            'observed_at':moment.isoformat(timespec='microseconds'),
            'until':until.isoformat(timespec='microseconds')}
    wire['hold_id'] = 'model-output-contract-hold:'+content_hash(wire)[:32]
    wire['content_hash'] = content_hash(wire)
    if preview:
        return wire
    with router._transaction() as cur:
        old = cur.execute('SELECT record_json FROM '+TABLE+' WHERE hold_id=?', (wire['hold_id'],)).fetchone()
        if old is not None:
            if old[0] != canonical_json(wire):
                raise ValueError('output-contract hold replay differs')
            return wire
        cur.execute('INSERT INTO '+TABLE+' (hold_id,scope_hash,profile_version_ref,route_ref,observed_at,until,content_hash,record_json) VALUES (?,?,?,?,?,?,?,?)',
                    (wire['hold_id'],wire['scope_hash'],profile['profile_version_ref'],route['id'],wire['observed_at'],wire['until'],wire['content_hash'],canonical_json(wire)))
    return wire


def record_after_settlement(*args: Any, **kwargs: Any) -> dict[str, Any] | None:
    # Advisory bookkeeping must not interrupt Scheduler completion or discard
    # paid telemetry if the router is busy. No hold is claimed when it fails.
    try:
        return record(*args, **kwargs)
    except Exception as exc:
        import sys
        print('cockpit-model: output-contract hold not recorded: '+type(exc).__name__, file=sys.stderr)
        return None
