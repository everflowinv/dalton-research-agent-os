"""One normal-budget continuation backed by an explicit human limit increase.

The grant never settles an old bill and never changes an old Work. Unrelated
policy revisions do not open another continuation for the same frozen scope.
"""
from __future__ import annotations
from datetime import datetime
from pathlib import Path
from .store import content_hash
from .shared_call_budget_policy import load_shared_call_budget_policy, effective_shared_limits

CONTRACT_REF = 'deep-insight-gate-human-budget-grant:0.1'
MAX_REVISIONS = 64
CALL_PURPOSES = ('deep_insight_gate', 'deep_insight_gate_verifier')


def limits(policy):
    return {**{p:effective_shared_limits(policy,p) for p in CALL_PURPOSES},
            'run':effective_shared_limits(policy,'deep_insight_gate',kind='run')}


def load_archived_policy(model, digest):
    from .workspace_host_scheme import resolve_manager_config
    from .workspace_manager import _config
    import re
    if re.fullmatch('[0-9a-f]{64}',str(digest)) is None:
        raise ValueError('deep-gate archived human policy hash is invalid')
    manager=resolve_manager_config(Path(model.scheduler_db).parent)
    if manager is None:raise ValueError('deep-gate host manager authority is unavailable')
    root=Path(_config(manager)['host_root'])/'shared-call-budget-revisions'
    saved=load_shared_call_budget_policy(root/(digest+'.json'))
    if saved['content_hash']!=digest:raise ValueError('deep-gate archived human policy identity differs')
    return saved


def grant_history(model, grant):
    # An already authorized grant rereads its exact immutable archive chain.
    # Unrelated later host edits cannot make it exceed a global census bound.
    refs=grant['human_policy_chain_hashes']
    if not refs or len(refs)>MAX_REVISIONS or refs[0]!=grant['human_policy_hash']:
        raise ValueError('deep-gate archived grant chain is invalid')
    return [load_archived_policy(model, ref) for ref in refs]


def current_policy(model):
    path=model.config.get('shared_call_budget_policy_path')
    if not path:raise ValueError('deep-gate current human policy is absent')
    return load_shared_call_budget_policy(path)


def policy_history(model):
    from .workspace_host_scheme import resolve_manager_config
    from .workspace_manager import _config
    manager=resolve_manager_config(Path(model.scheduler_db).parent)
    path=model.config.get('shared_call_budget_policy_path')
    if manager is None or not path:
        raise ValueError('deep-gate recovery needs the host-owned human budget policy')
    root=Path(_config(manager)['host_root'])/'shared-call-budget-revisions'
    current=load_shared_call_budget_policy(path)
    history=[current]
    for _ in range(MAX_REVISIONS):
        before=history[-1]
        if before['prior_hash'] is None:return history
        target=root/(before['prior_hash']+'.json')
        # The first seeded policy predates the revision archive. This is a
        # bounded chain suffix, not a claim that missing history is complete.
        # grant_proof still requires a policy active at the old admission.
        if not target.exists():return history
        previous=load_shared_call_budget_policy(target)
        if (previous['content_hash']!=before['prior_hash']
                or previous['revision']+1!=before['revision']
                or datetime.fromisoformat(previous['updated_at'])>datetime.fromisoformat(before['updated_at'])):
            raise ValueError('deep-gate shared human budget revision chain differs')
        history.append(previous)
    raise ValueError('deep-gate human budget history exceeds bounded64 revisions')


def grant_proof(*,scope_ref,entry,parent,formal,bill,history):
    from .shared_call_budget_policy import validate_shared_call_budget_policy
    checked=[validate_shared_call_budget_policy(p) for p in history]
    if not checked:raise ValueError('deep-gate human budget history is absent')
    for newer,older in zip(checked,checked[1:]):
        if newer['prior_hash']!=older['content_hash'] or newer['revision']!=older['revision']+1:
            raise ValueError('deep-gate human policy history has a gap')
    work=parent['work_order'];result=formal['result_envelope'];meta=result['metadata']
    failures=meta.get('chain_failures')or[]
    if (entry['role']!='normal' or work['metadata']['purpose']!='deep_insight_gate'
            or work['metadata'].get('company_ref') not in (None, entry.get('company_ref'))
            or content_hash(work['question'])!=entry['prompt_hash']
            or not work['metadata']['request_id'].startswith(entry['request_id'])
            or result['status']!='failed' or result['error']['code']!='MODEL_CHAIN_EXHAUSTED'
            or meta.get('control_plane_failure')is not True or not failures
            or any(f.get('code')!='TIMEOUT' or f.get('failure_class')!='unclassified_failure' for f in failures)
            or meta.get('dispatch_state')!='unknown' or not bill['unknown_fee_retained']
            or bill['work_ref']!=work['id'] or bill['work_hash']!=parent['work_order_hash']
            or bill['formal_hash']!=formal['content_hash']):
        raise ValueError('deep-gate grant requires the exact pure TIMEOUT terminal and retained bill')
    # The old budget is frozen in the Work. Find the human policy which was
    # already active when that Work was created, not a guessed old default.
    created=min(datetime.fromisoformat(f['admitted_at']) for f in bill['fees'])
    prior=next((p for p in checked if datetime.fromisoformat(p['updated_at'])<=created),None)
    if prior is None:raise ValueError('deep-gate old human budget authority is unavailable')
    old,new=limits(prior),limits(checked[0])
    old_call=old['deep_insight_gate'];new_call=new['deep_insight_gate']
    frozen={**work['budget'],'timeout_seconds':work['budget']['max_seconds']}
    if any(frozen[k]!=old_call.get(k) for k in ('max_input_tokens','max_output_tokens','max_cost_usd','timeout_seconds')):
        raise ValueError('deep-gate old Work budget differs from its human policy: '+str({k:(frozen[k],old_call.get(k)) for k in ('max_input_tokens','max_output_tokens','max_cost_usd','timeout_seconds') if frozen[k]!=old_call.get(k)}))
    if old['run'].get('max_units')!=4:
        raise ValueError('deep-gate legacy wanted groups lack the original four-group budget authority')
    if (not all(new_call.get(k,0)>=frozen[k] for k in ('max_input_tokens','max_output_tokens','max_cost_usd','timeout_seconds'))
            or new_call.get('timeout_seconds',0)<=frozen['timeout_seconds']
            or new['run'].get('max_cost_usd',0)<=old['run'].get('max_cost_usd',0)):
        raise ValueError('deep-gate human policy has not increased both timeout and original run allowance')
    # Scope and group, rather than any budget revision or limit values,
    # are the stable once-only identity.
    semantic={'contract_ref':CONTRACT_REF,'scope_ref':scope_ref,
        'group':entry['group'],'normal_request_id':entry['request_id']}
    identity='deep-gate-budget-grant:'+content_hash(semantic)
    body={**semantic,'limits':new,'id':identity,'parent_work_ref':work['id'],
        'parent_work_hash':parent['work_order_hash'],'parent_formal_hash':formal['content_hash'],
        'old_policy_hash':prior['content_hash'],'human_policy_hash':checked[0]['content_hash'],
        'human_policy_chain_hashes':[p['content_hash'] for p in checked[:checked.index(prior)+1]],
        'old_limits':old,'retained_parent_bill':bill,
        'request_id':entry['request_id']+':deep-gate-budget-grant:'+identity.rsplit(':',1)[1][:32]}
    return {**body,'content_hash':content_hash(body)}
