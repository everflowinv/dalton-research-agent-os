"""Unchanged failed screen inputs stay closed; wall-clock time grants no retry.

This local guard can only suppress a call. It is not financial evidence,
retry authority, a fee refund, or a replacement for formal/budget admission.
"""
from pathlib import Path
from .store import content_hash
from .initial_screen_failure_feedback import _read,_write,_body
from .product_contract import tag_entry
from .product_passage_validation import PRODUCT_VALIDATION_CONTRACT
from .initial_screen import STRL_CONTRACT_VERSION
SCHEMA='initial-screen-failed-input:1'
MAX_INPUTS=64


def execution_binding(model,state):
    """Actual screen routing/limits only; other-purpose settings do not wake it."""
    if not hasattr(model,'config') or not hasattr(model,'budget_for'):return None
    from .host_research_reconcile import resolved_model_config
    from .planner_paid_output_recovery import _authority_connection
    from .model_router import ModelRouter,declared_tier_chain
    from .model_fallback_chain import purpose_tiers
    from .call_budget import resolve_call_budget
    bindings={}
    for purpose in ('initial_screen','initial_screen_verifier'):
        config=resolved_model_config(state,model.config,purpose)
        with _authority_connection(config['model_router_db']) as connection:
            router=object.__new__(ModelRouter);router.connection=connection
            policy=router.get_policy(config['routing_policy_ref'])
        override=(policy.get('purpose_overrides') or {}).get(purpose)
        bindings[purpose]={'chain':override or declared_tier_chain(policy,purpose_tiers()[purpose]),
            'thinking':(policy.get('thinking_level_overrides') or {}).get(purpose),
            'budget':resolve_call_budget(config,purpose,defaults=model.budget_for(purpose)),
            'provider_retry':config.get('provider_retry'),
            'transport':{key:config.get(key) for key in ('capacity_retry','transport_retry','broker_max_frame_bytes')}}
    return bindings


def input_binding(mission,company,pack,prior,source_bindings,*,reopen=None,execution=None):
    # Quote changes, clocks and unrelated company counts do not repair a draft.
    rows=[]
    nonresearch=('pack-json:/numbers/market/','pack-json:/numbers/valuation/','pack-json:/numbers/peers/')
    for tag in sorted(pack.get('tags',{})):
        entry=tag_entry(pack,tag)
        if not entry:continue
        ref=entry['ref']
        if ref.startswith(nonresearch):continue
        semantic_entry={key:value for key,value in entry.items()
                        if key not in {'tag','generated_at','built_at','watermark','numbers_hash'}}
        rows.append({'ref':ref,'entry_hash':content_hash(semantic_entry)})
    rows=sorted(rows,key=lambda row:(row['ref'],row['entry_hash']))
    def without_clock(value, path=()):
        if isinstance(value,dict):
            return {key:without_clock(item,(*path,key)) for key,item in value.items()
                    if key not in {'as_of','generated_at','built_at','watermark'}
                    and not (path==('numbers','consensus','staleness') and
                             key in {'reference_date','vendor_days_old','report_days_old','stale'})}
        if isinstance(value,list):return [without_clock(item,path) for item in value]
        return value
    # Tag metadata is already covered by the actual resolved rows; numbers_hash
    # includes live prices and generation times and is not a research input.
    structured={key:without_clock(value,(key,)) for key,value in (pack.get('json') or {}).items()
                if key not in {'as_of','built_at','watermark','number_tags','passage_tags','numbers_hash'}}
    if isinstance(structured.get('numbers'),dict):
        structured['numbers']={key:value for key,value in structured['numbers'].items()
                               if key not in {'market','valuation','peers'}}
    body={'schema_version':SCHEMA,'validation_contract':PRODUCT_VALIDATION_CONTRACT,
        'producer_contract':STRL_CONTRACT_VERSION,
        'mission_ref':mission['id'],'mission_hash':mission['content_hash'],
        'company_ref':company['company_ref'],'recipe':pack['recipe_version'],'sources':source_bindings,
        'material_rows':rows,'structured_material_hash':content_hash(structured),
        'authorized_reopen_hash':content_hash(reopen),
        'execution_binding':execution,
        'prior_version_ref':(prior or {}).get('version_ref'),
        'prior_version_hash':(prior or {}).get('content_hash') or content_hash((prior or {}).get('sections',[]))}
    return {**body,'content_hash':content_hash(body)}


def _path(state,company):
    return Path(state)/'initial-screen-failed-inputs'/(content_hash(company)+'.json')


def unchanged_failure(state,binding):
    path=_path(state,binding['company_ref'])
    if not path.exists():return None
    held=_read(path);_body(held)
    if held.get('schema_version')!=SCHEMA or held.get('company_ref')!=binding['company_ref']:
        raise ValueError('initial-screen failed input guard differs')
    inputs=held['inputs']
    if not isinstance(inputs,list) or len(inputs)>MAX_INPUTS:raise ValueError('initial-screen failed input guard is unbounded')
    match=next((row for row in inputs if row['input_hash']==binding['content_hash']),None)
    if match is None and len(inputs)>=MAX_INPUTS:
        raise ValueError('initial-screen failed input guard is full; no new paid attempt')
    return match


def record_failure(state,summary):
    binding=summary.pop('_failed_input_binding',None)
    if not binding or summary.get('status')!='failed':return
    path=_path(state,binding['company_ref']);path.parent.mkdir(mode=0o700,exist_ok=True)
    previous=[]
    if path.exists():
        held=_read(path);_body(held);previous=held['inputs']
    if any(row['input_hash']==binding['content_hash'] for row in previous):return
    if len(previous)>=MAX_INPUTS:raise ValueError('initial-screen failed input guard is full')
    entry={'input_hash':binding['content_hash'],'source_binding_hash':content_hash(binding['sources']),
        'validation_contract':binding['validation_contract'],'terminal_status':'failed',
        'failure_feedback':summary.get('validation_feedback'),'failure_reason':str(summary.get('failure_reason',''))[:700]}
    body={'schema_version':SCHEMA,'company_ref':binding['company_ref'],'inputs':[entry,*previous]}
    _write(path,{**body,'content_hash':content_hash(body)})
