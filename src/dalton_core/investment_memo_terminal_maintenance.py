"""One fixed, current-authority continuation after the original technical slot.

Both terminal TIMEOUTs keep their unknown fee ceilings. A genuinely increased
shared timeout and run budget can admit one new Work, never reset old slots.
"""
from __future__ import annotations
import json
from pathlib import Path
from .store import canonical_json, content_hash

CONTRACT_REF = 'investment-memo-terminal-technical-maintenance:0.1'
MARKER = ':memo-terminal-maintenance:'
TABLE = 'investment_memo_terminal_maintenance_authorizations'
CLAIMS = 'investment_memo_terminal_maintenance_consumers'
MAX_UNUSED_CLAIMS = 64


def _context(prompt):
    from .investment_memo_draft import build_group_prompt
    company=json.loads(prompt.split('\nCompany: ',1)[1].split('\n',1)[0])
    group=prompt.split('\nGroup: ',1)[1].split('\n',1)[0]
    material=json.loads(prompt.split('\nEvidence rows:\n',1)[1].split('\nReturn raw JSON only',1)[0])
    contract=json.loads(prompt.split('Return raw JSON only with exactly this shape and the exact requested titles/questions:\n',1)[1].split('\n',1)[0])
    value={'company':company,'group':group,'material':material,
           'section_titles':[s['title'] for s in contract['sections']],
           'questions':[{k:q[k] for k in ('question_ref','question')} for q in contract['key_questions']]}
    if build_group_prompt(**value)!=prompt:raise ValueError('terminal memo original group prompt differs')
    return value


def _current(macro, context):
    """Current owner permissions, separate from original paid mission authority."""
    from .readonly_sqlite import connect_read_only
    from .coverage_mission import CoverageMissionAuthority
    from .initial_screen_failure_feedback import _checked_originals
    from .investment_memo_draft import citation_index
    from .product_passage_validation import rendered_passage_matches
    import sqlite3
    company=context['company']['company_ref'];mission=macro.mission
    with connect_read_only(Path(macro.model.scheduler_db).parent/'core.sqlite') as c:
        c.row_factory=sqlite3.Row
        owner=CoverageMissionAuthority.__new__(CoverageMissionAuthority);owner.connection=c
        current=owner._resolve_active_mission_for_company(company,purpose=CONTRACT_REF,
            mission_version_ref=mission['id'],mission_version_hash=mission['content_hash'])
        if (current!=mission or company!=macro.scope['company_ref']
                or 'investment_memo' not in current['deliverables']
                or 'deliverable' not in current['autonomy']['may_write']
                or 'investment_memo' not in current['autonomy']['human_checkpoints']):
            raise ValueError('terminal memo current permission differs')
        cur=c.cursor();owner._validate_playbook_binding(cur,current['bindings']['playbook_version'])
        owner._validate_constitution_binding(cur,current['bindings']['constitution_version'],current['industry_ref'])
        owner._validate_mandate_binding(cur,current['bindings']['mandate_version'],current['industry_ref'])
        passages=citation_index(context['material'])['passages']
        originals=_checked_originals(Path(macro.model.scheduler_db).parent,set(passages),
            include_render_metadata=True,proof_scope='initial_screen_checkpoint',include_source_metadata=True) if passages else {}
        grants={}
        for ref,text in passages.items():
            original=originals[ref]
            if not rendered_passage_matches(original['text'],text,original.get('render_header')):
                raise ValueError('terminal memo frozen passage differs from original')
            source=original['source_ref']
            grants[source]=owner.authorize_source_discovery(company_ref=company,source_ref=source,
                requested_by=current['autonomy']['automation_principal'],
                mission_version_ref=current['id'],mission_version_hash=current['content_hash'])
    return {'mission':{k:mission[k] for k in ('id','content_hash')},'source_grants':grants,
            'originals':{ref:{k:v for k,v in body.items() if k!='text'} for ref,body in originals.items()}}


def _policy(macro, wrapper, original):
    from .call_budget import _shared_policy,resolve_run_budget
    from .shared_call_budget_policy import effective_shared_limits
    policy=_shared_policy(macro.model.config)
    if policy is None:raise ValueError('terminal memo requires a saved shared budget increase')
    call=macro.model.budget_for('investment_memo')
    run=resolve_run_budget(macro.model.config,'investment_memo',defaults={'max_cost_usd':84,'max_units':4})
    shared_call=effective_shared_limits(policy,'investment_memo');shared_run=effective_shared_limits(policy,'investment_memo',kind='run')
    if (shared_call.get('timeout_seconds')!=call['timeout_seconds']
            or shared_run.get('max_cost_usd')!=run['max_cost_usd']
            or call['timeout_seconds']<=original['timeout_seconds']
            or run['max_cost_usd']*1_000_000<=original['run_micros']
            or int(round(run['max_cost_usd']*1_000_000))!=wrapper.run_micros
            or int(round(call['max_cost_usd']*1_000_000))!=wrapper.producer_micros):
        raise ValueError('terminal memo timeout and run budget have not both effectively increased')
    # Only these actual execution fields govern eligibility, never policy rev,
    # unrelated purposes, the day, runtime version, or mutable router state.
    return {'call':call,'run':run}


def qualify(macro, wrapper, connection):
    from .scheduler import Scheduler
    from .contracts import WorkOrder
    from .investment_memo_macro_accounting import _body,_works,EXTRA
    from .investment_memo_timeout_recovery import prompt_identity,verify_scope,verify_macro
    from .cockpit_timeout_recovery import _row,CONSUMERS,failure_proof
    from .investment_memo_legacy_accounting import bill
    from .planner_paid_output_recovery import recovery_readers
    row=connection.execute(f'SELECT * FROM {EXTRA} WHERE macro_ref=?',(macro.ref,)).fetchone()
    if row is None or _body(row).get('role')!='technical':raise ValueError('terminal memo original technical slot is absent')
    extra=_body(row);reader=Scheduler.__new__(Scheduler);reader.connection=connection
    parent=reader.work_order_authority(extra['parent_work_ref'])
    if parent is None:raise ValueError('terminal memo original parent absent')
    root=WorkOrder.from_dict(parent['work_order']);context=_context(root.question)
    _,_,index,material=prompt_identity(root.question)
    if (root.question!=macro.prompts[index] or context['company']['company_ref']!=macro.scope['company_ref']
            or material!=macro.scope['material_hash']):raise ValueError('terminal memo original macro scope differs')
    works=_works(connection,macro.scope['normal_requests'][index])
    successors=[(work,authority) for work,authority in works if work.metadata.get('timeout_recovery') is not None]
    if len(successors)!=1:raise ValueError('terminal memo does not have exactly one consumed original successor')
    child,child_authority=successors[0];record=child.metadata['timeout_recovery'];old_scope=record.get('memo_scope') or {}
    if (record['proof']['parent_work_ref']!=root.id or record['proof']['parent_work_hash']!=parent['work_order_hash']
            or extra['parent_failure_proof_hash']!=content_hash(record['proof'])
            or child.question!=root.question or old_scope.get('company_ref')!=macro.scope['company_ref']
            or old_scope.get('group_index')!=index or old_scope.get('material_hash')!=material):
        raise ValueError('terminal memo consumed successor is foreign')
    from .readonly_sqlite import connect_read_only
    from .coverage_mission import CoverageMissionAuthority
    import sqlite3
    with connect_read_only(Path(macro.model.scheduler_db).parent/'core.sqlite') as core:
        core.row_factory=sqlite3.Row
        owner=CoverageMissionAuthority.__new__(CoverageMissionAuthority);owner.connection=core
        old_mission=owner._mission_record(root.metadata['mission_version_ref'])
    if (old_mission['mission_ref']!=macro.mission['mission_ref']
            or context['company']['company_ref'] not in {v['company_ref'] for v in old_mission['universe']}
            or any(work.metadata.get('mission_version_ref')!=old_mission['id'] or
                   work.metadata.get('mission_version_hash')!=old_mission['content_hash'] for work in (root,child))):
        raise ValueError('terminal memo paid prefix belongs to another mission')
    consumer=connection.execute(f'SELECT * FROM {CONSUMERS} WHERE authorization_id=?',(record['id'],)).fetchone()
    if (consumer is None or consumer['work_ref']!=child.id or consumer['work_hash']!=child_authority['work_order_hash']):
        raise ValueError('terminal memo original technical successor was not consumed')
    with recovery_readers(macro.model.config['model_router_db'],macro.model.config['budget_db'],now=macro.model.clock()) as (router,budget):
        if budget is None:raise ValueError('terminal memo fee authority absent')
        if _row(connection,record['semantic_hash'])!=record:
            raise ValueError('terminal memo original authorization is not authentic')
        root_proof=failure_proof(connection,router,budget,work_ref=root.id,mission=old_mission)
        if root_proof!=record['proof']:raise ValueError('terminal memo original pure TIMEOUT differs')
        # The consumed original successor proves when this old scope was issued.
        # Replay that immutable preceding census; the current macro separately
        # retains every subsequent paid/unknown charge, including maintenance.
        verify_scope(connection,router,budget,old_scope,root_proof,mission=old_mission,
                     through_rowid=child_authority['append_rowid']-1)
        verify_macro(connection,record)
        formal=reader.formal_result(child.id)
        if formal is None or formal['terminal_state']!='failed':raise ValueError('terminal memo successor still pending or did not fail')
        wire=formal['result_envelope'];md=wire.get('metadata') or {};failures=md.get('chain_failures')
        if ((wire.get('error') or {}).get('code')!='MODEL_CHAIN_EXHAUSTED'
                or md.get('control_plane_failure') is not True or md.get('dispatch_state')!='unknown'
                or not isinstance(failures,list) or not failures
                or any(f.get('code')!='TIMEOUT' or f.get('failure_class')!='unclassified_failure' for f in failures)):
            raise ValueError('terminal memo successor is not a pure terminal TIMEOUT')
        paid=[bill(connection,router,budget,parent,reader.formal_result(root.id)),
              bill(connection,router,budget,child_authority,formal)]
        if any(not p['fees'] or any(f['settlement_ref'] is None for f in p['fees']) for p in paid):
            raise ValueError('terminal memo original fee settlement is incomplete')
    for other, _ in works:
        if other.id not in {root.id,child.id} and MARKER not in other.metadata.get('request_id',''):
            raise ValueError('terminal memo original group has additional unproved attempts')
    original={'timeout_seconds':max(root.budget['max_seconds'],child.budget['max_seconds']),
              'run_micros':old_scope['run_micros']}
    return {'contract_ref':CONTRACT_REF,'permission_ref':macro.ref+MARKER+'fixed',
            'macro_ref':macro.ref,'company_ref':macro.scope['company_ref'],'material_hash':material,
            'group_index':index,'prompt':root.question,'parent':paid[0],'used_successor':paid[1],
            'original_authorization_hash':record['content_hash'],'original_limits':original,
            'current_authority':_current(macro,context)}


def _schema(cur):
    for table,key,columns in ((TABLE,'permission_ref','macro_binding TEXT NOT NULL, record_json TEXT NOT NULL, content_hash TEXT NOT NULL'),
                             (CLAIMS,'claim_ref','permission_ref TEXT NOT NULL, work_ref TEXT UNIQUE NOT NULL, work_hash TEXT NOT NULL')):
        cur.execute(f'CREATE TABLE IF NOT EXISTS {table} ({key} TEXT PRIMARY KEY,{columns})')
        cur.execute(f'CREATE TRIGGER IF NOT EXISTS {table}_insert_owner BEFORE INSERT ON {table} '
                    "WHEN dalton_scheduler_authorized()!=1 BEGIN SELECT RAISE(ABORT,'Scheduler owner required'); END")
        for action in ('UPDATE','DELETE'):
            cur.execute(f'CREATE TRIGGER IF NOT EXISTS {table}_{action.lower()} BEFORE {action} ON {table} '
                        "BEGIN SELECT RAISE(ABORT,'terminal memo maintenance is append-only'); END")


class Authority:
    def __init__(self,macro,wrapper,body,policy):self.macro=macro;self.wrapper=wrapper;self.body=body;self.policy=policy
    def verify_child(self,scheduler,work):
        from .investment_memo_macro_accounting import _body,_record
        fresh=qualify(self.macro,self.wrapper,scheduler.connection)
        if fresh!=self.body or _policy(self.macro,self.wrapper,fresh['original_limits'])!=self.policy:
            raise ValueError('terminal memo current authority or effective budget drifted')
        expected=self.macro.scope['normal_requests'][fresh['group_index']]['request_id']+MARKER+content_hash(fresh)[:32]
        if (not work.metadata.get('request_id','').startswith(expected)
                or work.question!=fresh['prompt'] or work.metadata.get('purpose')!='investment_memo'
                or work.metadata.get('mission_version_ref')!=self.macro.mission['id']
                or work.metadata.get('mission_version_hash')!=self.macro.mission['content_hash']
                or work.budget!={'max_input_tokens':self.policy['call']['max_input_tokens'],
                    'max_output_tokens':self.policy['call']['max_output_tokens'],
                    'max_seconds':self.policy['call']['timeout_seconds'],
                    'max_cost_usd':self.policy['call']['max_cost_usd'],
                    'max_total_tokens':self.policy['call']['max_input_tokens']+self.policy['call']['max_output_tokens']}):
            raise ValueError('terminal memo child Work differs')
        with scheduler._transaction() as cur:
            _schema(cur)
            prior=cur.execute(f'SELECT * FROM {TABLE} WHERE permission_ref=?',(fresh['permission_ref'],)).fetchone()
            if prior is None:
                _record(cur,TABLE,'permission_ref',fresh['permission_ref'],self.macro.ref,
                        {'proof':fresh,'effective_limits_at_issue':self.policy})
            elif _body(prior)['proof']!=fresh:
                raise ValueError('terminal memo persisted current authority differs')
            claims=cur.execute(f'SELECT * FROM {CLAIMS} WHERE permission_ref=? LIMIT ?',
                               (fresh['permission_ref'],MAX_UNUSED_CLAIMS+1)).fetchall()
            if len(claims)>MAX_UNUSED_CLAIMS or (len(claims)==MAX_UNUSED_CLAIMS and
                    not any(row['work_ref']==work.id for row in claims)):
                raise ValueError('terminal memo unused-claim capacity exhausted')
            for row in claims:
                if row['work_ref']==work.id:
                    if row['work_hash']!=content_hash(work.to_dict()):raise ValueError('terminal memo exact Work changed')
                elif not _no_send(scheduler.connection,self.macro.model,row):
                    raise ValueError('terminal memo maintenance already consumed by paid or unknown Work')
            cur.execute(f'INSERT OR IGNORE INTO {CLAIMS} VALUES(?,?,?,?)',
                (fresh['permission_ref']+':'+work.id,fresh['permission_ref'],work.id,content_hash(work.to_dict())))


def _no_send(connection,model,claim):
    """Only exact atomic local budget refusal leaves this one grant unused."""
    from .scheduler import Scheduler
    from .contracts import WorkOrder,ResultEnvelope
    from .cockpit_timeout_recovery import budget_refusal_proof
    from .planner_paid_output_recovery import recovery_readers
    reader=Scheduler.__new__(Scheduler);reader.connection=connection
    authority=reader.work_order_authority(claim['work_ref']);formal=reader.formal_result(claim['work_ref'])
    if (authority is None or authority['work_order_hash']!=claim['work_hash']
            or formal is None or formal['terminal_state']!='failed'):return False
    work=WorkOrder.from_dict(authority['work_order'])
    result=ResultEnvelope.from_dict(formal['result_envelope'])
    with recovery_readers(model.config['model_router_db'],model.config['budget_db'],now=model.clock()) as (router,budget):
        if budget is None or budget.connection.execute('SELECT 1 FROM thesis_impact_day_admissions WHERE work_order_ref=? LIMIT 1',(work.id,)).fetchone():return False
        from .investment_memo_legacy_accounting import bill
        bill(connection,router,budget,authority,formal)
        return budget_refusal_proof(budget,router,work,formal['attempt_number'],
            {'result':result,'invocation':None,'cost_micros':0,
             'pool_rejection':result.metadata.get('pool_rejection')}) is not None


def recover(wrapper,exc,kwargs):
    from .cockpit_model import model_failure_trace,CockpitModelError
    from .coverage_mission import CoverageMissionError
    import sqlite3
    from .scheduler import Scheduler
    from .investment_memo_macro_accounting import _body
    from .planner_paid_output_recovery import _authority_connection
    macro=wrapper.macro
    if (macro is None or not wrapper.stage or not wrapper.stage['allow_technical']
            or kwargs['purpose']!='investment_memo' or model_failure_trace(exc) is None):return None
    from .call_budget import _shared_policy
    from .shared_call_budget_policy import effective_shared_limits
    shared=_shared_policy(macro.model.config)
    if (shared is None or 'timeout_seconds' not in effective_shared_limits(shared,'investment_memo')
            or 'max_cost_usd' not in effective_shared_limits(shared,'investment_memo',kind='run')):
        return None  # Ordinary old technical policy is untouched.
    try:
        with _authority_connection(macro.model.scheduler_db) as c:
            body=qualify(macro,wrapper,c)
            if kwargs['prompt']!=body['prompt']:return None
            policy=_policy(macro,wrapper,body['original_limits'])
            saved=None
            if c.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(TABLE,)).fetchone():
                row=c.execute(f'SELECT * FROM {TABLE} WHERE permission_ref=?',(body['permission_ref'],)).fetchone()
                if row is not None:
                    saved=_body(row)
                    if saved['proof']!=body:raise ValueError('terminal memo persisted proof differs')
            request=macro.scope['normal_requests'][body['group_index']]['request_id']+MARKER+content_hash(body)[:32]
            new_kwargs={**kwargs,'request_id':request}
            # Full original census before any new fee; old unknown ceilings,
            # all unfinished groups and independent verifier remain retained.
            macro.before_paid(new_kwargs,run_micros=wrapper.run_micros,
                producer_micros=wrapper.producer_micros,verifier_micros=wrapper.verifier_micros,role='maintenance')
            if saved is not None:
                claims=c.execute(f'SELECT * FROM {CLAIMS} WHERE permission_ref=? LIMIT ?',
                    (body['permission_ref'],MAX_UNUSED_CLAIMS+1)).fetchall()
                if not claims or len(claims)>MAX_UNUSED_CLAIMS:raise ValueError('terminal memo consumed permission lacks bounded Work proof')
                for claim in claims:
                    if _no_send(c,macro.model,claim):continue
                    reader=Scheduler.__new__(Scheduler);reader.connection=c
                    authority=reader.work_order_authority(claim['work_ref']);formal=reader.formal_result(claim['work_ref'])
                    if (authority is None or authority['work_order_hash']!=claim['work_hash'] or formal is None):
                        raise ValueError('terminal memo maintenance is already in flight or unproved')
                    work=authority['work_order']
                    if work['question']!=body['prompt']:raise ValueError('terminal memo saved Work prompt differs')
                    from .contracts import WorkOrder
                    return macro.model._answer(formal,WorkOrder.from_dict(work),True,0,'replayed')
        if wrapper.stage.get('quiesce') is not None:wrapper.stage['quiesce'].checkpoint()
        authority=Authority(macro,wrapper,body,policy)
        from .cockpit_model import CockpitModel
        model=wrapper.model
        while not isinstance(model,CockpitModel):model=model.model
        model._memo_terminal_maintenance_authority=authority
        try:return wrapper.model.call(**new_kwargs)
        finally:del model._memo_terminal_maintenance_authority
    except (ValueError,OSError,KeyError,TypeError,sqlite3.Error,CoverageMissionError) as failure:
        raise CockpitModelError('memo_terminal_maintenance_unavailable: '+str(failure)) from failure


def persisted_entry(macro,connection,index=None):
    from .investment_memo_macro_accounting import _body
    if not connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",(TABLE,)).fetchone():return None
    row=connection.execute(f'SELECT * FROM {TABLE} WHERE permission_ref=?',
        (macro.ref+MARKER+'fixed',)).fetchone()
    if row is None:return None
    saved=_body(row);proof=saved['proof']
    if proof!=qualify(macro,None,connection):raise ValueError('terminal memo persisted source/current authority differs')
    if index is not None and proof['group_index']!=index:return None
    return {'role':'terminal_maintenance','group_index':proof['group_index'],'purpose':'investment_memo',
        'request_id':macro.scope['normal_requests'][proof['group_index']]['request_id']+MARKER+content_hash(proof)[:32],
        'prompt_hash':content_hash(proof['prompt'])},proof


def call_existing(wrapper,kwargs):
    from .planner_paid_output_recovery import _authority_connection
    from .investment_memo_macro_accounting import AUDIT_ERRORS
    from .cockpit_model import CockpitModel,CockpitModelError
    from .coverage_mission import CoverageMissionError
    try:
        with _authority_connection(wrapper.macro.model.scheduler_db) as connection:
            saved=persisted_entry(wrapper.macro,connection)
            if saved is None:raise ValueError('terminal memo permission absent')
            entry,body=saved
            if kwargs['request_id']!=entry['request_id'] or kwargs['prompt']!=body['prompt']:
                raise ValueError('terminal memo resumed request differs')
            policy=_policy(wrapper.macro,wrapper,body['original_limits'])
        authority=Authority(wrapper.macro,wrapper,body,policy)
        transport=wrapper.model
        while not isinstance(transport,CockpitModel):transport=transport.model
        transport._memo_terminal_maintenance_authority=authority
        try:return wrapper.model.call(**kwargs)
        finally:del transport._memo_terminal_maintenance_authority
    except (*AUDIT_ERRORS,CoverageMissionError) as exc:
        raise CockpitModelError('memo_terminal_maintenance_unavailable: '+str(exc)) from exc
