"""Fixed once-per-macro capability continuation of a strong technical failure.

Only a profile-limit normal group or the registered semantic step TIMEOUT
qualifies. Old paid/unknown fees remain in the original macro census. Active
broker limits are read again before any new Work, never inferred from a miss.
"""
from pathlib import Path
from .store import content_hash
from .investment_memo_terminal_maintenance import _context, _current, _no_send
from .openclaw_broker_capabilities import read_profile_limits
from .openclaw_model_adapter import OpenClawModelAdapterError
CONTRACT_REF='investment-memo-profile-capability-maintenance:0.1'
MARKER=':memo-profile-maintenance:'
TABLE='investment_memo_profile_maintenance_authorizations'
CLAIMS='investment_memo_profile_maintenance_consumers'
MAX_UNUSED_CLAIMS=64


def qualify(macro, wrapper, connection):
    from .scheduler import Scheduler
    from .investment_memo_macro_accounting import _works,EXTRA,REQUESTS,_body
    from .investment_memo_legacy_accounting import bill
    from .planner_paid_output_recovery import recovery_readers
    from .investment_memo_timeout_recovery import prompt_identity
    if not macro.complete:raise ValueError('profile memo original census incomplete')
    issued=None
    if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(TABLE,)).fetchone():
        row=connection.execute(f'SELECT * FROM {TABLE} WHERE permission_ref=?',(macro.ref+MARKER+'fixed',)).fetchone()
        if row is not None:issued=_body(row)['proof']
    issued_profile=issued is not None and issued['required_limits']['failure_code']=='PROFILE_LIMIT_EXCEEDED'
    extra_row=connection.execute(f'SELECT * FROM {EXTRA} WHERE macro_ref=?',(macro.ref,)).fetchone()
    extra=None if extra_row is None else _body(extra_row)
    if extra is not None and extra.get('role')!='semantic' and not (issued_profile and extra.get('role')=='technical'):
        raise ValueError('capability memo has another consumed technical slot')
    entries=list(macro.scope['normal_requests'])
    if extra is not None and extra.get('role')=='semantic':
        rows=connection.execute(f'SELECT * FROM {REQUESTS} WHERE macro_binding=? AND request_key=?',(macro.ref,macro.ref+':'+extra['request_id'])).fetchall()
        if len(rows)!=1:raise ValueError('capability memo semantic registration absent')
        semantic=_body(rows[0])
        if (semantic['role']!='semantic' or semantic['prompt_hash']!=extra['prompt_hash']
                or semantic['request_id']!=extra['request_id'] or semantic['purpose']!='investment_memo'
                or type(semantic['group_index']) is not int or not 0<=semantic['group_index']<len(macro.prompts)):
            raise ValueError('capability memo semantic extra differs')
        entries.append(semantic)
    reader=Scheduler.__new__(Scheduler);reader.connection=connection
    parents=[]
    with recovery_readers(macro.model.config['model_router_db'],macro.model.config['budget_db'],now=macro.model.clock()) as (router,budget):
        if budget is None:raise ValueError('profile memo original fee authority absent')
        if issued_profile and extra is not None:
            _successful_issued_parent(macro,connection,router,budget,reader,issued)
            if extra['role']=='technical':
                from .cockpit_timeout_recovery import failure_proof
                parent=reader.work_order_authority(extra['parent_work_ref'])
                if parent is None:raise ValueError('profile memo later technical parent absent')
                held=parent['work_order']
                technical=failure_proof(connection,router,budget,work_ref=extra['parent_work_ref'],mission=macro.mission)
                if (held['question'] not in macro.prompts or extra['parent_work_ref']==issued['parent']['work_ref']
                        or technical is None or content_hash(technical)!=extra['parent_failure_proof_hash']):
                    raise ValueError('profile memo later technical slot differs')
        for entry in entries:
            for work,authority in _works(connection,entry):
                if MARKER in work.metadata.get('request_id',''):continue
                formal=reader.formal_result(work.id)
                if formal is None:raise ValueError('profile memo original Work still in flight')
                paid=bill(connection,router,budget,authority,formal)
                if issued is not None and work.id!=issued['parent']['work_ref'] and entry['role']=='normal':
                    if (work.question!=macro.prompts[entry['group_index']]
                            or work.metadata.get('mission_version_ref')!=macro.mission['id']
                            or work.metadata.get('mission_version_hash')!=macro.mission['content_hash']):
                        raise ValueError('profile memo later normal Work differs')
                    # Later independent failures remain fully accounted; they
                    # cannot be selected as another historical grant parent.
                    continue
                if issued_profile and entry['role']=='semantic':
                    from .investment_memo_macro_accounting import request_id
                    from .investment_memo_draft import GROUPS
                    index=entry['group_index']
                    if (not work.question.startswith(macro.prompts[index]+'\nYour previous answer for this group was refused and was not used:\n')
                            or entry['request_id']!=request_id(GROUPS[index][0],work.question)
                            or work.metadata.get('mission_version_ref')!=macro.mission['id']
                            or work.metadata.get('mission_version_hash')!=macro.mission['content_hash']):
                        raise ValueError('capability memo later semantic Work differs')
                    # This existing ordinary slot retains its own success/failure
                    # and fees. It cannot replace the historical grant parent.
                    continue
                if formal['terminal_state']=='succeeded':continue
                wire=formal['result_envelope'];md=wire.get('metadata') or {};failures=md.get('chain_failures')
                expected_code='PROFILE_LIMIT_EXCEEDED' if (extra is None or issued_profile) and entry['role']=='normal' else 'TIMEOUT' if extra is not None and entry['role']=='semantic' else None
                if ((wire.get('error') or {}).get('code')!='MODEL_CHAIN_EXHAUSTED'
                        or md.get('control_plane_failure') is not True or md.get('dispatch_state')!='unknown'
                        or not isinstance(failures,list) or not failures
                        or expected_code is None or any(f.get('code')!=expected_code or f.get('failure_class')!='unclassified_failure' for f in failures)
                        or not paid['fees'] or any(f['settlement_ref'] is None for f in paid['fees'])):
                    raise ValueError('profile memo needs one pure settled profile-limit parent')
                if (not (work.question in macro.prompts if entry['role']=='normal' else work.question.startswith(macro.prompts[entry['group_index']]+'\nYour previous answer for this group was refused and was not used:\n')) or work.metadata.get('purpose')!='investment_memo'
                        or work.metadata.get('mission_version_ref')!=macro.mission['id']
                        or work.metadata.get('mission_version_hash')!=macro.mission['content_hash']):
                    raise ValueError('profile memo parent current mission or prompt differs')
                if len({f.get('profile_id') for f in failures})!=1:
                    raise ValueError('profile memo multiple failed capabilities are ambiguous')
                route=router.get_decision(wire['metadata']['route_decision_ref'])
                from .model_router import _profile_wire
                selected=router.get_profile(route['selected_profile_version_ref'])
                if _profile_wire(selected)!=selected:raise ValueError('capability memo selected profile hash differs')
                if selected['id']!=failures[0]['profile_id']:raise ValueError('profile memo failed route identity differs')
                parents.append((work,paid,failures[0]['profile_id'],{**route,'selected_model':selected['provider']+'/'+selected['model']},expected_code,entry['request_id']))
    if len(parents)!=1:raise ValueError('profile memo does not have exactly one failed normal parent')
    root,paid,profile,route,code,parent_request=parents[0]
    _,_,index,material=prompt_identity(root.question);context=_context(macro.prompts[index])
    if context['company']['company_ref']!=macro.scope['company_ref'] or material!=macro.scope['material_hash']:
        raise ValueError('profile memo original macro scope differs')
    # The immutable typed formal proves the historical refusal, not zero send.
    # No historical numeric limit is fabricated from the error's generic text.
    proof={'contract_ref':CONTRACT_REF,'permission_ref':macro.ref+MARKER+'fixed',
        'macro_ref':macro.ref,'company_ref':macro.scope['company_ref'],'material_hash':material,
        'group_index':index,'prompt':root.question,'parent':paid,'parent_request_id':parent_request,
        'required_limits':{'timeout_ms':root.budget['max_seconds']*1000,
                           'max_tokens':root.budget['max_output_tokens'],'profile_id':profile,
                           'model_ref':route['selected_model'],'failure_code':code},
        'current_authority':_current(macro,context)}
    if issued is not None and proof!=issued:raise ValueError('profile memo original issued parent differs')
    return proof


def _successful_issued_parent(macro,connection,router,budget,reader,proof):
    """A later ordinary semantic slot cannot manufacture a PROFILE grant."""
    claims=connection.execute(f'SELECT * FROM {CLAIMS} WHERE permission_ref=? LIMIT ?',
        (proof['permission_ref'],MAX_UNUSED_CLAIMS+1)).fetchall()
    if not claims or len(claims)>MAX_UNUSED_CLAIMS:raise ValueError('profile memo prior maintenance success absent')
    paid_success=[]
    prefix=proof['parent_request_id']+MARKER+content_hash(proof)[:32]
    for claim in claims:
        if _no_send(connection,macro.model,claim):continue
        authority=reader.work_order_authority(claim['work_ref'])
        if authority is None or authority['work_order_hash']!=claim['work_hash']:
            raise ValueError('profile memo prior maintenance Work differs')
        from .contracts import WorkOrder
        from .investment_memo_legacy_accounting import bill
        work=WorkOrder.from_dict(authority['work_order']);formal=reader.formal_result(work.id)
        if formal is None:raise ValueError('profile memo prior maintenance success has no terminal formal')
        paid=bill(connection,router,budget,authority,formal)
        if (formal['terminal_state']!='succeeded' or work.question!=proof['prompt']
                or work.metadata.get('purpose')!='investment_memo'
                or not work.metadata.get('request_id','').startswith(prefix)
                or work.metadata.get('mission_version_ref')!=macro.mission['id']
                or work.metadata.get('mission_version_hash')!=macro.mission['content_hash']
                or paid['unknown_fee_retained'] or not paid['fees']
                or any(f['settlement_ref'] is None for f in paid['fees'])):
            raise ValueError('profile memo prior maintenance success unproved')
        paid_success.append(work.id)
    if len(paid_success)!=1:raise ValueError('profile memo prior maintenance success is not unique')


def _policy(macro,wrapper,required):
    from .call_budget import _shared_policy,resolve_run_budget
    from .shared_call_budget_policy import effective_shared_limits
    shared=_shared_policy(macro.model.config)
    if shared is None:raise ValueError('profile memo saved owner budget absent')
    call=macro.model.budget_for('investment_memo')
    run=resolve_run_budget(macro.model.config,'investment_memo',defaults={'max_cost_usd':70,'max_units':4})
    if (effective_shared_limits(shared,'investment_memo').get('timeout_seconds')!=call['timeout_seconds']
            or effective_shared_limits(shared,'investment_memo',kind='run').get('max_cost_usd')!=run['max_cost_usd']
            or call['timeout_seconds']*1000<required['timeout_ms']
            or (required['failure_code']=='TIMEOUT' and call['timeout_seconds']*1000<=required['timeout_ms'])
            or call['max_output_tokens']<required['max_tokens']
            or int(round(run['max_cost_usd']*1_000_000))!=wrapper.run_micros
            or int(round(call['max_cost_usd']*1_000_000))!=wrapper.producer_micros):
        raise ValueError('profile memo current effective owner budget does not cover original request')
    capability=read_profile_limits(macro.model.config,profile_id=required['profile_id'],model_ref=required['model_ref'])
    if (capability['limits']['timeoutMs']<call['timeout_seconds']*1000
            or capability['limits']['maxTokens']<call['max_output_tokens']):
        raise ValueError('profile memo active broker still does not support requested limits')
    return {'call':call,'run':run,'active_capability':capability}


def _schema(cur):
    for table,key,columns in ((TABLE,'permission_ref','macro_binding TEXT NOT NULL, record_json TEXT NOT NULL, content_hash TEXT NOT NULL'),
                             (CLAIMS,'claim_ref','permission_ref TEXT NOT NULL, work_ref TEXT UNIQUE NOT NULL, work_hash TEXT NOT NULL')):
        cur.execute(f'CREATE TABLE IF NOT EXISTS {table} ({key} TEXT PRIMARY KEY,{columns})')
        cur.execute(f'CREATE TRIGGER IF NOT EXISTS {table}_insert_owner BEFORE INSERT ON {table} '
                    "WHEN dalton_scheduler_authorized()!=1 BEGIN SELECT RAISE(ABORT,'Scheduler owner required'); END")
        for action in ('UPDATE','DELETE'):
            cur.execute(f'CREATE TRIGGER IF NOT EXISTS {table}_{action.lower()} BEFORE {action} ON {table} '
                        "BEGIN SELECT RAISE(ABORT,'profile memo maintenance is append-only'); END")


def _request_id(macro, connection, proof):
    """Daily local refusals may defer this fixed permission, never a paid call."""
    from .scheduler import Scheduler
    from .planner_paid_output_recovery import recovery_readers
    base=proof['parent_request_id']+MARKER+content_hash(proof)[:32]
    if not connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(CLAIMS,)).fetchone():return base
    claims=connection.execute(f'SELECT * FROM {CLAIMS} WHERE permission_ref=? ORDER BY rowid LIMIT ?',
        (proof['permission_ref'],MAX_UNUSED_CLAIMS+1)).fetchall()
    if len(claims)>MAX_UNUSED_CLAIMS:raise ValueError('profile memo unused-claim capacity exhausted')
    if not claims or any(not _no_send(connection,macro.model,claim) for claim in claims):return base
    if len(claims)==MAX_UNUSED_CLAIMS:raise ValueError('profile memo unused-claim capacity exhausted')
    reader=Scheduler.__new__(Scheduler);reader.connection=connection
    claim=claims[-1];work=reader.work_order_authority(claim['work_ref'])['work_order']
    formal=reader.formal_result(claim['work_ref']);wire=formal['result_envelope']
    table='model_budget_pool_rejections' if wire['error']['code']=='POOL_EXHAUSTED' else 'thesis_impact_day_rejections'
    with recovery_readers(macro.model.config['model_router_db'],macro.model.config['budget_db'],now=macro.model.clock()) as (_,budget):
        # _no_send already checked the complete hash, route, all zero admissions
        # and this exact unique rejection. The day here only controls deferral.
        rows=budget.connection.execute(f'SELECT day FROM {table} WHERE work_order_ref=? AND attempt_number=? AND phase=?',
            (claim['work_ref'],formal['attempt_number'],'assessment')).fetchall()
        if len(rows)!=1:raise ValueError('profile memo no-send rejection differs')
    day=macro.model.clock().date().isoformat()
    return base+':no-send-day:'+day if day>rows[0]['day'] else work['metadata']['request_id']


class Authority:
    def __init__(self,macro,wrapper,body,policy):self.macro=macro;self.wrapper=wrapper;self.body=body;self.policy=policy
    def verify_child(self,scheduler,work):
        from .investment_memo_macro_accounting import _body,_record
        fresh=qualify(self.macro,self.wrapper,scheduler.connection)
        if fresh!=self.body or _policy(self.macro,self.wrapper,fresh['required_limits'])!=self.policy:
            raise ValueError('profile memo current authority or effective budget drifted')
        expected=_request_id(self.macro,scheduler.connection,fresh)
        if (not work.metadata.get('request_id','').startswith(expected)
                or work.question!=fresh['prompt'] or work.metadata.get('purpose')!='investment_memo'
                or work.metadata.get('mission_version_ref')!=self.macro.mission['id']
                or work.metadata.get('mission_version_hash')!=self.macro.mission['content_hash']
                or work.budget!={'max_input_tokens':self.policy['call']['max_input_tokens'],
                    'max_output_tokens':self.policy['call']['max_output_tokens'],
                    'max_seconds':self.policy['call']['timeout_seconds'],
                    'max_cost_usd':self.policy['call']['max_cost_usd'],
                    'max_total_tokens':self.policy['call']['max_input_tokens']+self.policy['call']['max_output_tokens']}):
            raise ValueError('profile memo child Work differs')
        with scheduler._transaction() as cur:
            _schema(cur)
            prior=cur.execute(f'SELECT * FROM {TABLE} WHERE permission_ref=?',(fresh['permission_ref'],)).fetchone()
            if prior is None:
                _record(cur,TABLE,'permission_ref',fresh['permission_ref'],self.macro.ref,
                        {'proof':fresh,'effective_limits_at_issue':self.policy})
            elif _body(prior)['proof']!=fresh:
                raise ValueError('profile memo persisted current authority differs')
            claims=cur.execute(f'SELECT * FROM {CLAIMS} WHERE permission_ref=? LIMIT ?',
                               (fresh['permission_ref'],MAX_UNUSED_CLAIMS+1)).fetchall()
            if len(claims)>MAX_UNUSED_CLAIMS or (len(claims)==MAX_UNUSED_CLAIMS and
                    not any(row['work_ref']==work.id for row in claims)):
                raise ValueError('profile memo unused-claim capacity exhausted')
            for row in claims:
                if row['work_ref']==work.id:
                    if row['work_hash']!=content_hash(work.to_dict()):raise ValueError('profile memo exact Work changed')
                elif not _no_send(scheduler.connection,self.macro.model,row):
                    raise ValueError('profile memo maintenance already consumed by paid or unknown Work')
            cur.execute(f'INSERT OR IGNORE INTO {CLAIMS} VALUES(?,?,?,?)',
                (fresh['permission_ref']+':'+work.id,fresh['permission_ref'],work.id,content_hash(work.to_dict())))


def recover(wrapper,exc,kwargs):
    from .cockpit_model import model_failure_trace,CockpitModelError
    from .coverage_mission import CoverageMissionError
    import sqlite3
    from .scheduler import Scheduler
    from .investment_memo_macro_accounting import _body,EXTRA
    from .planner_paid_output_recovery import _authority_connection
    macro=wrapper.macro
    if (macro is None or not wrapper.stage or not wrapper.stage['allow_technical']
            or kwargs['purpose']!='investment_memo' or model_failure_trace(exc) is None):return None
    trace=model_failure_trace(exc)
    with _authority_connection(macro.model.scheduler_db) as connection:
        reader=Scheduler.__new__(Scheduler);reader.connection=connection
        formal=reader.formal_result(trace['work_order_ref'])
        failures=[] if formal is None else formal['result_envelope'].get('metadata',{}).get('chain_failures',[])
        if not failures or len({f.get('code') for f in failures})!=1 or failures[0].get('code') not in {'PROFILE_LIMIT_EXCEEDED','TIMEOUT'}:return None
        if failures[0].get('code')=='TIMEOUT':
            extra=connection.execute(f'SELECT * FROM {EXTRA} WHERE macro_ref=?',(macro.ref,)).fetchone()
            if extra is None or _body(extra).get('role')!='semantic':return None
        authority=reader.work_order_authority(trace['work_order_ref'])
        if (authority is None or authority['work_order_hash']!=trace['work_order_hash']
                or formal['result_envelope_hash']!=trace['formal_result_envelope_hash']):
            raise CockpitModelError('memo_profile_maintenance_unavailable: failure trace differs')
    try:
        with _authority_connection(macro.model.scheduler_db) as c:
            body=qualify(macro,wrapper,c)
            if kwargs['prompt']!=body['prompt']:return None
            policy=_policy(macro,wrapper,body['required_limits'])
            saved=None
            if c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(TABLE,)).fetchone():
                row=c.execute(f'SELECT * FROM {TABLE} WHERE permission_ref=?',(body['permission_ref'],)).fetchone()
                if row is not None:
                    saved=_body(row)
                    if saved['proof']!=body:raise ValueError('profile memo persisted proof differs')
            request=_request_id(macro,c,body)
            new_kwargs={**kwargs,'request_id':request}
            # Full original census before any new fee; old unknown ceilings,
            # all unfinished groups and independent verifier remain retained.
            macro.before_paid(new_kwargs,run_micros=wrapper.run_micros,
                producer_micros=wrapper.producer_micros,verifier_micros=wrapper.verifier_micros,role='maintenance')
            if saved is not None:
                claims=c.execute(f'SELECT * FROM {CLAIMS} WHERE permission_ref=? LIMIT ?',
                    (body['permission_ref'],MAX_UNUSED_CLAIMS+1)).fetchall()
                if not claims or len(claims)>MAX_UNUSED_CLAIMS:raise ValueError('profile memo consumed permission lacks bounded Work proof')
                for claim in claims:
                    if _no_send(c,macro.model,claim):continue
                    reader=Scheduler.__new__(Scheduler);reader.connection=c
                    authority=reader.work_order_authority(claim['work_ref']);formal=reader.formal_result(claim['work_ref'])
                    if (authority is None or authority['work_order_hash']!=claim['work_hash'] or formal is None):
                        raise ValueError('profile memo maintenance is already in flight or unproved')
                    work=authority['work_order']
                    if work['question']!=body['prompt']:raise ValueError('profile memo saved Work prompt differs')
                    from .contracts import WorkOrder
                    return macro.model._answer(formal,WorkOrder.from_dict(work),True,0,'replayed')
        if wrapper.stage.get('quiesce') is not None:wrapper.stage['quiesce'].checkpoint()
        authority=Authority(macro,wrapper,body,policy)
        from .cockpit_model import CockpitModel
        model=wrapper.model
        while not isinstance(model,CockpitModel):model=model.model
        model._memo_profile_maintenance_authority=authority
        try:return wrapper.model.call(**new_kwargs)
        finally:del model._memo_profile_maintenance_authority
    except (ValueError,OSError,KeyError,TypeError,sqlite3.Error,CoverageMissionError,OpenClawModelAdapterError) as failure:
        raise CockpitModelError('memo_profile_maintenance_unavailable: '+str(failure)) from failure


def persisted_entry(macro,connection,index=None):
    from .investment_memo_macro_accounting import _body
    if not connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(TABLE,)).fetchone():return None
    row=connection.execute(f'SELECT * FROM {TABLE} WHERE permission_ref=?',
        (macro.ref+MARKER+'fixed',)).fetchone()
    if row is None:return None
    saved=_body(row);proof=saved['proof']
    if proof!=qualify(macro,None,connection):raise ValueError('profile memo persisted source/current authority differs')
    if index is not None and proof['group_index']!=index:return None
    return {'role':'profile_maintenance','group_index':proof['group_index'],'purpose':'investment_memo',
        'request_id':_request_id(macro,connection,proof),
        'prompt_hash':content_hash(proof['prompt'])},proof


def call_existing(wrapper,kwargs):
    from .planner_paid_output_recovery import _authority_connection
    from .investment_memo_macro_accounting import AUDIT_ERRORS
    from .cockpit_model import CockpitModel,CockpitModelError
    from .coverage_mission import CoverageMissionError
    try:
        with _authority_connection(wrapper.macro.model.scheduler_db) as connection:
            saved=persisted_entry(wrapper.macro,connection)
            if saved is None:raise ValueError('profile memo permission absent')
            entry,body=saved
            if kwargs['request_id']!=entry['request_id'] or kwargs['prompt']!=body['prompt']:
                raise ValueError('profile memo resumed request differs')
            policy=_policy(wrapper.macro,wrapper,body['required_limits'])
        authority=Authority(wrapper.macro,wrapper,body,policy)
        transport=wrapper.model
        while not isinstance(transport,CockpitModel):transport=transport.model
        transport._memo_profile_maintenance_authority=authority
        try:return wrapper.model.call(**kwargs)
        finally:del transport._memo_profile_maintenance_authority
    except (*AUDIT_ERRORS,CoverageMissionError,OpenClawModelAdapterError) as exc:
        raise CockpitModelError('memo_profile_maintenance_unavailable: '+str(exc)) from exc
