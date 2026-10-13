"""One explicit shared-policy second correction per exact macro/group.

Old fixed permissions, prompts, fees and unknown ceilings remain immutable.
Only the owner's first maintenance answer can parent this additional round.
"""
from .store import content_hash
from .call_budget import MEMO_MAINTENANCE_ROUNDS_FIELD

CONTRACT_REF = 'investment-memo-second-content-maintenance:0.1'


def scheduling_rounds(config):
    """Stable shared admin semantics; never a deployment/day wake generation."""
    from .shared_call_budget_policy import load_shared_call_budget_policy
    path=config.get('shared_call_budget_policy_path')
    if path is None:
        return 1
    value=load_shared_call_budget_policy(path)
    return value.get('purpose_run_limits',{}).get('investment_memo',{}).get(MEMO_MAINTENANCE_ROUNDS_FIELD,1)


def policy(model):
    """Only the current installed shared administrator policy can enable two."""
    from .shared_call_budget_policy import load_shared_call_budget_policy, MEMO_MAINTENANCE_SCHEMA_VERSION
    path=model.config.get('shared_call_budget_policy_path')
    if path is None:
        raise ValueError('second memo correction is not explicitly enabled in the shared administrator policy')
    value=load_shared_call_budget_policy(path)
    if (value['schema_version']!=MEMO_MAINTENANCE_SCHEMA_VERSION
            or value.get('purpose_run_limits',{}).get('investment_memo',{}).get(MEMO_MAINTENANCE_ROUNDS_FIELD)!=2):
        raise ValueError('shared memo policy still permits only one correction per group')
    return value


def _prove_policy(value):
    from .shared_call_budget_policy import validate_shared_call_budget_policy, MEMO_MAINTENANCE_SCHEMA_VERSION
    checked=validate_shared_call_budget_policy(value)
    if (checked['schema_version']!=MEMO_MAINTENANCE_SCHEMA_VERSION
            or checked.get('purpose_run_limits',{}).get('investment_memo',{}).get(MEMO_MAINTENANCE_ROUNDS_FIELD)!=2):
        raise ValueError('issued second memo correction has no explicit shared two-round policy')
    return checked


def derive(macro, context, work_ref, *, through_rowid=None, _issued_diagnostics=None,
           _issued_policy=None):
    from .investment_memo_group_maintenance import (
        _context,_parent,_proof_body,_validation_contracts,record_for,_replay_issued)
    from .planner_paid_output_recovery import _authority_connection
    index=_context(macro,context)
    with _authority_connection(macro.model.scheduler_db) as connection:
        first=record_for(macro,connection,index,round_number=1)
    if first is None or first['context']!=context or _replay_issued(macro,first)!=first:
        raise ValueError('second memo correction original first-round authority differs')
    granted=policy(macro.model) if _issued_policy is None else _prove_policy(_issued_policy)
    parent=_parent(macro,context,work_ref,through_rowid=through_rowid,
        _issued_diagnostics=_issued_diagnostics,_first_maintenance=first['permission_ref'])
    body=_proof_body(macro,context,parent,contract_ref=CONTRACT_REF,
        validation_contracts=_validation_contracts())
    body['permission_ref']=first['permission_ref']+':round2'
    body.update(round_number=2,prior_permission_ref=first['permission_ref'],
        prior_permission_hash=content_hash(first),shared_policy=granted)
    return body


def replay_issued(macro, record, *, include_parent=False):
    """Historical owner rows remain audit/replayable after the policy is reduced."""
    from .investment_memo_group_maintenance import _parent,_validate_issued_diagnostics
    from .planner_paid_output_recovery import _authority_connection
    from .investment_memo_macro_accounting import _body
    if record.get('contract_ref')!=CONTRACT_REF or record.get('round_number')!=2:
        raise ValueError('memo second correction contract/round is unknown')
    policy_record=_prove_policy(record['shared_policy'])
    # This private issued-diagnostic route is used only after the stored owner
    # row's hash and registration have been proved by the caller. It cannot
    # issue a new correction under a previous policy or validator.
    with _authority_connection(macro.model.scheduler_db) as connection:
        from .investment_memo_group_maintenance import TABLE
        row=connection.execute(f'SELECT * FROM {TABLE} WHERE permission_ref=?',
                               (record['prior_permission_ref'],)).fetchone()
        if row is None or content_hash(_body(row))!=record['prior_permission_hash']:
            raise ValueError('memo second correction first owner hash changed')
    parent=_parent(macro,record['context'],record['parent']['work_order_ref'],
        through_rowid=record['parent_append_rowid'],_issued_diagnostics=record['diagnostics'],
        _first_maintenance=record['prior_permission_ref'])
    _validate_issued_diagnostics(record['diagnostics'],record['context'],parent[1]['text'])
    rebuilt=derive(macro,record['context'],record['parent']['work_order_ref'],
        through_rowid=record['parent_append_rowid'],_issued_diagnostics=record['diagnostics'],
        _issued_policy=policy_record)
    if rebuilt['validation_contracts']!=record['validation_contracts']:
        # Known historical contracts decide authorization history; current
        # validation separately decides whether the returned text is usable.
        from .investment_memo_group_maintenance import _validation_contracts
        known={'number-source-contract:0.8','number-source-contract:0.9',
               'number-source-contract:0.10','number-source-contract:0.11'}
        if (record['validation_contracts'][:2]!=_validation_contracts()[:2]
                or len(record['validation_contracts'])!=3 or record['validation_contracts'][2] not in known):
            raise ValueError('memo second correction issued numeric validator is unknown')
        rebuilt['validation_contracts']=record['validation_contracts']
    return (rebuilt,parent) if include_parent else rebuilt


def call_existing(model, macro, record, kwargs, *, run_micros, producer_micros, verifier_micros):
    """Deny-only guard at every ordinary internal paid attempt, not a grant."""
    import copy
    from .investment_memo_failure_cost import MemoFailureCostReporter
    from .planner_paid_output_recovery import _authority_connection, recovery_readers
    from .investment_memo_group_maintenance import TABLE
    from .investment_memo_macro_accounting import _body,_works
    from .cockpit_model import _transport_request_id
    total,future=macro.room(group_index=record['entry']['group_index'],
        producer_micros=producer_micros,verifier_micros=verifier_micros)
    future=max(future,(3-record['entry']['group_index'])*producer_micros+verifier_micros)
    baseline={bill['work_ref']:bill['accounted_micros'] for bill in macro.last['bills']}
    with _authority_connection(macro.model.config['budget_db']) as fees:
        watermark={ref:fees.execute('SELECT MAX(rowid) FROM thesis_impact_day_admissions '
            'WHERE work_order_ref=?',(ref,)).fetchone()[0] or 0 for ref in baseline}
    target=copy.copy(model)
    if isinstance(model,MemoFailureCostReporter):
        target.model=copy.copy(model.model);base=target.model
    else:
        base=target
    prior=getattr(base,'_run_cost_admission_guard',None)
    def guard(*,work,attempt_number,reserved_micros,mission_binding):
        if prior is not None:
            prior(work=work,attempt_number=attempt_number,reserved_micros=reserved_micros,
                  mission_binding=mission_binding)
        policy(macro.model)
        from .host_research_reconcile import resolved_model_config
        from pathlib import Path
        config=resolved_model_config(Path(macro.model.scheduler_db).parent,macro.model.config,'investment_memo')
        if (work.question!=record['prompt'] or work.metadata.get('purpose')!='investment_memo'
                or work.metadata.get('request_id')!=_transport_request_id(record['entry']['request_id'],config)
                or (mission_binding.get('mission_version_ref'),mission_binding.get('mission_version_hash'))
                    !=(macro.mission['id'],macro.mission['content_hash'])
                or (work.metadata.get('mission_version_ref'),work.metadata.get('mission_version_hash'))
                    !=(macro.mission['id'],macro.mission['content_hash'])
                or type(reserved_micros)is not int or not 0<=reserved_micros<=producer_micros):
            raise ValueError('second memo correction paid child differs from its fixed owner scope')
        with _authority_connection(macro.model.scheduler_db) as owner:
            row=owner.execute(f'SELECT * FROM {TABLE} WHERE permission_ref=?',(record['permission_ref'],)).fetchone()
            if row is None or _body(row)!=record:
                raise ValueError('second memo correction owner authorization changed before admission')
            works=_works(owner,record['entry'])
            if not works or any(w.id!=work.id for w,_ in works):
                raise ValueError('second memo correction already has a different immutable child')
            if any(content_hash(w.to_dict())!=content_hash(work.to_dict()) for w,_ in works):
                raise ValueError('second memo correction child hash changed before admission')
            from .investment_memo_macro_accounting import REQUESTS,MAX_TOTAL_REQUESTS
            rows=owner.execute(f'SELECT * FROM {REQUESTS} WHERE macro_binding=? LIMIT ?',
                (macro.ref,MAX_TOTAL_REQUESTS+1)).fetchall()
            if len(rows)>MAX_TOTAL_REQUESTS:raise ValueError('second memo correction macro index exceeds bound')
            entries=[_body(row) for row in rows]+macro._legacy_entries(owner,check_growth=False)
            if any(candidate.id!=work.id and candidate.id not in baseline
                    for entry in entries for candidate,_ in _works(owner,entry)):
                raise ValueError('second memo correction macro gained another unaccounted active child')
        # All old bills were completely proved before entering this call. Each
        # new attempt retains its full ceiling even if its last telemetry is
        # smaller; an unknown admission can never become spare repair money.
        charged=0
        with recovery_readers(macro.model.config['model_router_db'],macro.model.config['budget_db'],
                              now=macro.model.clock()) as (router,budget):
            if budget is None:raise ValueError('second memo correction fee authority is unavailable')
            if any(ref!=work.id and (budget.connection.execute(
                    'SELECT MAX(rowid) FROM thesis_impact_day_admissions WHERE work_order_ref=?',
                    (ref,)).fetchone()[0] or 0)>mark for ref,mark in watermark.items()):
                raise ValueError('second memo correction other macro fees changed during this call')
            rows=budget.connection.execute('SELECT attempt_number,phase FROM thesis_impact_day_admissions '
                'WHERE work_order_ref=? ORDER BY attempt_number,phase LIMIT 65',(work.id,)).fetchall()
            if len(rows)>64:raise ValueError('second memo correction attempt fees exceed the exact read bound')
            for row in rows:
                paid=budget.admission(work_order_ref=work.id,attempt_number=row['attempt_number'],phase=row['phase'])
                admission,settlement=paid['admission'],paid['settlement'];route=router.get_decision(admission['route_decision_ref'])
                bound=paid['mission_binding'] or {}
                if (route['work_order_ref']!=work.id or route['work_order_hash']!=content_hash(work.to_dict())
                        or route['attempt_number']!=row['attempt_number']
                        or (bound.get('mission_version_ref'),bound.get('mission_version_hash'))
                            !=(macro.mission['id'],macro.mission['content_hash'])):
                    raise ValueError('second memo correction prior fee/mission/route differs')
                amount=admission['reserved_micros'] if settlement is None else settlement['actual_micros']
                if type(amount)is not int or amount<0:raise ValueError('second memo correction prior fee is invalid')
                charged+=max(producer_micros,amount)
        if total-baseline.get(work.id,0)+charged+max(producer_micros,reserved_micros)+future>run_micros:
            raise ValueError('second memo correction internal retry exceeds old fees and retained future room')
    base._run_cost_admission_guard=guard
    return target.call(**kwargs)
