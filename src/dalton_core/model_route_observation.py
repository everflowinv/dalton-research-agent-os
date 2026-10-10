"""Accounting-backed observations for the extractor's single-route execution.

These are display/audit facts, never routing inputs or permission to retry.
Historical replay is not stamped as a new provider call.
"""
from __future__ import annotations
import re
import sqlite3
from typing import Any, Mapping
from .store import content_hash

_TASKS=frozenset({'model-task:mission-document-extraction:0.1',
                 'task:document-numeric-extraction:0.1',
                 'task:metric-discovery-extraction:0.2'})
_HASH=re.compile(r'[a-f0-9]{64}')


def record_extraction_single_route(router: Any, *, work: Any, route: Mapping[str, Any],
        profile: Mapping[str, Any], invocation: Any, result: Any,
        replayed: bool = False) -> dict[str, Any]:
    """Append the exact already-accounted returned route, with bounded skips.

    A returned response is not an accepted Claim. Unknown/no-send/estimated
    usage and foreign tasks are not represented as served calls. Existing chain
    execution already owns its records; this function only fills single-route
    execution and never changes a frozen Work or any provider/fee record.
    """
    meta=work.metadata or {}
    if (meta.get('task_ref') not in _TASKS
            or meta.get('purpose') not in {None,'document_extraction'}):
        return {'status':'not_applicable'}
    if replayed:
        return {'status':'historical_replay_not_backfilled'}
    use=invocation.usage or {};rmeta=result.metadata or {}
    telemetry=use.get('raw_provider_telemetry') or {};response=rmeta.get('broker_response_hash')
    if (invocation.actor_ref!='runtime:openclaw-model-broker'
            or invocation.completed_at is None or use.get('metering_source')!='provider_reported'
            or rmeta.get('control_plane_failure') is True
            or rmeta.get('dispatch_proof') is not None
            or not isinstance(response,str) or not _HASH.fullmatch(response)
            or telemetry.get('response_content_hash')!=response
            or type(use.get('input_tokens')) is not int or type(use.get('output_tokens')) is not int
            or use['input_tokens']<0 or use['output_tokens']<0):
        return {'status':'no_returned_provider_proof'}
    if (router.get_decision(route['id'])!=dict(route)
            or router.get_profile(profile['profile_version_ref'])!=dict(profile)
            or route.get('outcome')!='selected'
            or route.get('work_order_ref')!=work.id
            or route.get('work_order_hash')!=content_hash(work.to_dict())
            or route.get('selected_profile_version_ref')!=profile['profile_version_ref']
            or route.get('selected_profile_hash')!=profile['content_hash']
            or invocation.work_order_ref!=work.id or result.work_order_ref!=work.id
            or result.invocation_ref!=invocation.id
            or invocation.profile_ref!=profile['profile_version_ref']
            or invocation.provider!=profile['provider'] or invocation.model!=profile['model']
            or invocation.model_family!=profile['family'] or invocation.runtime_ref!=profile['adapter_ref']
            or rmeta.get('route_decision_ref')!=route['id']
            or rmeta.get('profile_version_ref')!=profile['profile_version_ref']):
        raise ValueError('accounted extraction route observation binding differs')
    from .model_router import resolve_chain
    from .model_fallback_chain import tier_for
    policy=router.get_policy(route['policy_version_ref'])
    if policy['content_hash']!=route['policy_hash']:
        raise ValueError('accounted extraction observation policy differs')
    profiles={};snapshots={}
    for item in route.get('candidate_snapshot') or []:
        held=router.get_profile(item['profile_version_ref'])
        if held['content_hash']!=item['profile_hash']:
            raise ValueError('accounted extraction observation candidate differs')
        profiles[held['id']]=held;snapshots[held['id']]=item
    tier=tier_for('document_extraction')
    resolved=resolve_chain(policy,tier=tier,purpose='document_extraction',profiles=profiles)
    if resolved is None or profile['id'] not in resolved['chain']:
        return {'status':'no_declared_chain_position'}
    chain=list(resolved['chain']);position=chain.index(profile['id'])+1
    # Existing chain execution may have written the served row while the
    # router excluded a held predecessor before entering execute_chain. Exact
    # duplicate link identity is handled by record_chain_link below; fill only
    # the missing evidence-backed predecessors, not a second served event.
    count=0
    try:
        for index,identity in enumerate(chain[:position],1):
            reason=None
            if identity!=profile['id']:
                rejected=(snapshots.get(identity) or {}).get('rejection_reasons') or []
                if not rejected:continue
                reason=('paid_output_contract_hold' if 'paid_output_contract_hold' in rejected
                        else 'provider_cooldown' if 'provider_cooldown' in rejected else str(rejected[0]))
            row=router.record_chain_link(work_order_id=work.id,capability=route['capability'],
                attempt_number=route['attempt_number'],purpose='document_extraction',tier=tier,
                chain_position=index,profile_id=identity,decision_id=route['id'],
                policy_version_ref=route['policy_version_ref'],served=identity==profile['id'],skip_reason=reason)
            if row['status']=='conflict':return {'status':'observation_conflict'}
            count+=row['status']=='fresh'
    except sqlite3.Error:
        # A display ledger must not prevent the normally accounted result from
        # completing or cause another provider call after a store-lock failure.
        return {'status':'observation_store_unavailable'}
    return {'status':'recorded','new_links':count}


def output_contract_hold_view(router: Any, *, policy: Mapping[str, Any], purpose: str,
        profiles: Mapping[str, Mapping[str, Any]], call_budget: Mapping[str, Any],
        thinking_level: str, now: Any) -> dict[str, dict[str, Any]]:
    """Only current exact-scope holds; these are not global provider health."""
    from .model_output_contract_hold import active
    needed={'max_input_tokens','max_output_tokens','max_cost_usd','timeout_seconds'}
    if not needed<=set(call_budget):return {}
    budget={key:call_budget[key] for key in needed-{'timeout_seconds'}}
    budget.update(max_total_tokens=budget['max_input_tokens']+budget['max_output_tokens'],
                  max_seconds=call_budget['timeout_seconds'])
    held={}
    for identity,profile in profiles.items():
        record=active(router.connection,profile,policy_ref=policy['policy_version_ref'],
            policy_hash=policy['content_hash'],purpose=purpose,budget=budget,thinking_level=thinking_level,now=now)
        if record is not None:
            held[identity]={'kind':'paid_output_contract_hold','scope':'purpose_budget',
                'observed_at':record['observed_at'],'until':record['until'],
                'hold_ref':record['hold_id'],'note':'本环节这组上限下曾收到超额输出，暂按保存顺序使用其它模型；其它环节不受此限制。'}
    return held


def last_output_hold_skip_note(router: Any, served: Mapping[str, Any] | None) -> str | None:
    """Describe the last returned route's skips as history, never active health."""
    if served is None:return None
    route=router.get_decision(served['decision_id'])
    if (route['outcome']!='selected'
            or route['policy_version_ref']!=served['policy_version_ref']):
        raise ValueError('served extraction observation route differs')
    links=router.chain_links(work_order_id=route['work_order_ref'])
    if any(link['decision_id']==route['id'] and link['purpose']=='document_extraction'
           and not link['served'] and link['skip_reason']=='paid_output_contract_hold'
           for link in links):
        return '上次调用曾因本环节输出超限暂避前面的模型；这是该次调用记录，不是当前全局冷却。'
    return None
