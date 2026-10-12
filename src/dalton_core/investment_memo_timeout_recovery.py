"""One technical timeout continuation inside the memo's existing extra-call slot.

A timeout does not prove no-send. Its full macro reservation and immutable
ledger settlement remain charged. Successful groups replay their formal calls.
"""
from __future__ import annotations
import json
from .contracts import WorkOrder, ResultEnvelope
from .store import content_hash

CONTRACT_REF = 'investment-memo-timeout-continuation:0.1'
MACRO_TABLE = 'investment_memo_timeout_macro_authorizations'


def prompt_identity(prompt):
    from .investment_memo_draft import GROUPS
    try:
        company = json.loads(prompt.split('\nCompany: ', 1)[1].split('\n', 1)[0])
        group = prompt.split('\nGroup: ', 1)[1].split('\n', 1)[0]
        material = json.loads(prompt.split('\nEvidence rows:\n', 1)[1].split(
            '\nReturn raw JSON only', 1)[0])
    except (ValueError, IndexError, KeyError) as exc:
        raise ValueError('memo timeout parent has no exact group/input binding') from exc
    groups = [item[0] for item in GROUPS]
    if group not in groups or not company.get('company_ref'):
        raise ValueError('memo timeout parent is not a known company group')
    return company, group, groups.index(group), content_hash(material)


def _successful(connection, router, budget, call, mission, *, group, company_ref, material_hash):
    from .scheduler import Scheduler
    reader = Scheduler.__new__(Scheduler); reader.connection = connection
    authority = reader.work_order_authority(call['work_order_ref'])
    formal = reader.formal_result(call['work_order_ref'])
    if authority is None or formal is None:
        raise ValueError('memo timeout completed group authority is missing')
    work = WorkOrder.from_dict(authority['work_order'])
    company, held_group, _, held_material = prompt_identity(work.question)
    result = ResultEnvelope.from_dict(formal['result_envelope'])
    if (held_group != group or company['company_ref'] != company_ref or held_material != material_hash
            or work.metadata.get('purpose') != 'investment_memo'
            or work.metadata.get('mission_version_ref') != mission['id']
            or work.metadata.get('mission_version_hash') != mission['content_hash']
            or content_hash(result.to_dict()) != formal['result_envelope_hash']
            or formal['terminal_state'] != 'succeeded' or result.status != 'succeeded'
            or result.work_order_ref != work.id or result.id != call['result_envelope_ref']
            or result.invocation_ref != call['invocation_ref']
            or result.metadata.get('route_decision_ref') != call['route_decision_ref']):
        raise ValueError('memo timeout completed group binding differs')
    body = {k:formal[c] for k,c in (
        ('id','result_record_id'),('work_order_id','work_order_id'),('attempt_number','attempt_number'),
        ('result_envelope_id','result_envelope_id'),('result_envelope_hash','result_envelope_hash'),
        ('terminal_state','terminal_state'),('created_at','created_at'))}
    if content_hash(body) != formal['content_hash']:
        raise ValueError('memo timeout completed formal hash differs')
    route = router.get_decision(call['route_decision_ref'])
    if (route['work_order_ref'] != work.id or route['work_order_hash'] != authority['work_order_hash']
            or route['attempt_number'] != formal['attempt_number'] or route['outcome'] != 'selected'):
        raise ValueError('memo timeout completed route differs')
    rows = budget.connection.execute('SELECT attempt_number,phase FROM thesis_impact_day_admissions '
        'WHERE work_order_ref=? ORDER BY attempt_number,phase', (work.id,)).fetchall()
    fees = []; total = 0; selected = False
    for row in rows:
        paid = budget.admission(work_order_ref=work.id, attempt_number=row['attempt_number'], phase=row['phase'])
        settlement = paid['settlement']; binding = paid['mission_binding'] or {}
        if (settlement is None or binding.get('mission_version_ref') != mission['id']
                or binding.get('mission_version_hash') != mission['content_hash']):
            raise ValueError('memo timeout completed group fees are not settled')
        admission = paid['admission']; held = router.get_decision(admission['route_decision_ref'])
        if (held['work_order_ref'] != work.id or held['work_order_hash'] != authority['work_order_hash']
                or held['attempt_number'] != row['attempt_number']):
            raise ValueError('memo timeout completed fee route differs')
        selected |= row['attempt_number'] == formal['attempt_number']
        total += settlement['actual_micros']
        fees.append({'admission_ref':admission['admission_id'],'admission_hash':admission['content_hash'],
            'settlement_ref':settlement['settlement_id'],'settlement_hash':settlement['content_hash']})
    if not selected:
        raise ValueError('memo timeout completed group has no selected settlement')
    return {'work_ref':work.id,'work_hash':authority['work_order_hash'],
            'formal_hash':formal['content_hash'],'fees':fees,'accounted_micros':total}


def _historical_bill(connection, budget, work_ref):
    from .scheduler import Scheduler
    reader=Scheduler.__new__(Scheduler);reader.connection=connection
    authority=reader.work_order_authority(work_ref)
    work=WorkOrder.from_dict(authority['work_order'])
    formal=reader.formal_result(work.id)
    rows=budget.connection.execute('SELECT attempt_number,phase FROM thesis_impact_day_admissions '
        'WHERE work_order_ref=? ORDER BY attempt_number,phase',(work.id,)).fetchall()
    bills=[]; amount=0; missions=set()
    if formal is None:
        raise ValueError('memo timeout prior matching group still has unsettled authority')
    for row in rows:
        paid=budget.admission(work_order_ref=work.id,attempt_number=row['attempt_number'],phase=row['phase'])
        admission,settlement=paid['admission'],paid['settlement']
        missions.add((paid['mission_binding'] or {}).get('mission_ref'))
        if settlement is None:
            raise ValueError('memo timeout prior matching group still has unsettled authority')
        amount+=settlement['actual_micros']
        bills.append({'admission_ref':admission['admission_id'],'admission_hash':admission['content_hash'],
            'settlement_ref':settlement['settlement_id'],'settlement_hash':settlement['content_hash']})
    if formal is not None:
        envelope=ResultEnvelope.from_dict(formal['result_envelope'])
        body={k:formal[c] for k,c in (
            ('id','result_record_id'),('work_order_id','work_order_id'),('attempt_number','attempt_number'),
            ('result_envelope_id','result_envelope_id'),('result_envelope_hash','result_envelope_hash'),
            ('terminal_state','terminal_state'),('created_at','created_at'))}
        if (content_hash(body)!=formal['content_hash'] or envelope.work_order_ref!=work.id
                or content_hash(envelope.to_dict())!=formal['result_envelope_hash']):
            raise ValueError('memo timeout historical fee formal binding differs')
        if envelope.metadata.get('dispatch_state')=='unknown':
            amount=max(amount,int(round(work.budget['max_cost_usd']*1_000_000)))
    if len(missions)>1:
        raise ValueError('memo timeout historical fees span different missions')
    return {'work_ref':work.id,'work_hash':authority['work_order_hash'],'fees':bills,
            'mission_ref':next(iter(missions),None),'accounted_micros':amount}

def historical_costs(connection, budget, work_refs, *, mission_ref, excluded_timeout_parent=None, through_rowid=None):
    """Bounded request-index seeks, including prior budget/mission variants.

    These are bills only: no earlier failed/running answer is ever reused.
    The current group's technical child is reserved below, not double counted.
    """
    import re
    from .scheduler import Scheduler
    reader = Scheduler.__new__(Scheduler); reader.connection = connection
    found = {}
    for ref in work_refs:
        anchor = WorkOrder.from_dict(reader.work_order_authority(ref)['work_order'])
        prefix = re.match(r'memo-[0-9a-f]{24}', anchor.metadata['request_id'])
        if prefix is None:
            raise ValueError('memo timeout parent has no exact content-addressed group request')
        key = prefix.group()
        if through_rowid is not None and (type(through_rowid) is not int or through_rowid < 1):
            raise ValueError('memo timeout historical append boundary is invalid')
        boundary = '' if through_rowid is None else ' AND rowid<=?'
        params = (key,key+'\uffff') if through_rowid is None else (key,key+'\uffff',through_rowid)
        ids = connection.execute("SELECT work_order_id FROM scheduler_work_orders WHERE "
            "json_extract(work_order_json,'$.metadata.request_id')>=? AND "
            "json_extract(work_order_json,'$.metadata.request_id')<?"+boundary+" LIMIT 17", params).fetchall()
        if len(ids)>16:
            raise ValueError('memo timeout group history exceeds the bounded16 Work authority read')
        for row in ids:
            held = reader.work_order_authority(row['work_order_id'])
            work = WorkOrder.from_dict(held['work_order'])
            if work.question != anchor.question or work.metadata.get('purpose') != 'investment_memo':
                continue
            recovery = work.metadata.get('timeout_recovery') or {}
            if recovery.get('proof',{}).get('parent_work_ref') == excluded_timeout_parent:
                continue
            bill=_historical_bill(connection,budget,work.id)
            if bill['mission_ref']==mission_ref:
                found[work.id]=bill
    return [found[key] for key in sorted(found)]

def verify_scope(connection, router, budget, scope, proof, *, mission, through_rowid=None):
    """The original extra call slot, exact preceding groups, and full worst-case bill."""
    from .scheduler import Scheduler
    from .investment_memo_draft import GROUPS
    fields={'contract_ref','macro_ref','company_ref','group','group_index','material_hash',
            'max_units','max_repairs','run_micros','producer_micros','verifier_micros',
            'completed','accounted_micros','completed_proofs','historical_costs'}
    if set(scope)!=fields or scope.get('contract_ref') != CONTRACT_REF or scope.get('max_repairs') != 1:
        raise ValueError('memo timeout recovery has no original single extra-call slot')
    for field in ('run_micros','producer_micros','verifier_micros','max_units'):
        if type(scope.get(field)) is not int or scope[field] < 1:
            raise ValueError('memo timeout macro limits are invalid')
    reader = Scheduler.__new__(Scheduler); reader.connection = connection
    parent = WorkOrder.from_dict(reader.work_order_authority(proof['parent_work_ref'])['work_order'])
    company, group, index, material_hash = prompt_identity(parent.question)
    if ('Your previous answer for this group was refused and was not used:' in parent.question
            or parent.metadata.get('timeout_recovery') is not None
            or scope.get('company_ref') != company['company_ref'] or scope.get('group') != group
            or scope.get('group_index') != index or scope.get('material_hash') != material_hash
            or scope.get('max_units') < len(GROUPS) or len(scope.get('completed') or []) != index):
        raise ValueError('memo timeout recovery is not the exact normal producer stage')
    key = content_hash({'company_ref':company['company_ref'], 'mission_ref':mission['mission_ref'],
                        'material_hash':material_hash})
    if scope.get('macro_ref') != 'memo-timeout-macro:' + key:
        raise ValueError('memo timeout macro identity differs')
    completed = [_successful(connection,router,budget,call,mission,group=GROUPS[i][0],
        company_ref=company['company_ref'],material_hash=material_hash)
        for i,call in enumerate(scope['completed'])]
    # The old unknown producer keeps its full *macro* reservation, even if
    # its conservatively settled route estimate was lower. No refund is made.
    history=historical_costs(connection,budget,
        [call['work_order_ref'] for call in scope['completed']]+[parent.id],
        mission_ref=mission['mission_ref'],excluded_timeout_parent=parent.id,through_rowid=through_rowid)
    if (history!=scope.get('historical_costs') or len(history)>48
            or parent.id not in {h['work_ref']for h in history}
            or any(h['mission_ref']!=mission['mission_ref']for h in history)):
        raise ValueError('memo timeout historical macro bills differ')
    accounted=max(sum(item['accounted_micros'] for item in history),
        sum(item['accounted_micros'] for item in completed)+max(scope['producer_micros'],proof['accounted_micros']))
    needed = accounted + (len(GROUPS)-index) * scope['producer_micros'] + scope['verifier_micros']
    if scope.get('accounted_micros') < accounted or needed > scope['run_micros']:
        raise ValueError('memo timeout continuation exceeds the original macro budget')
    if scope.get('completed_proofs') != completed:
        raise ValueError('memo timeout completed group proofs differ')
    return scope


def verify_macro(connection, record):
    scope = record.get('memo_scope')
    if scope is None:
        return
    row = connection.execute('SELECT * FROM '+MACRO_TABLE+' WHERE macro_ref=?',
                             (scope['macro_ref'],)).fetchone()
    if row is None or row['authorization_id'] != record['id'] or row['authorization_hash'] != record['content_hash']:
        raise ValueError('memo timeout extra-call slot is not bound to this authorization')


def reserve_macro(cur, record):
    scope = record['memo_scope']
    cur.execute('CREATE TABLE IF NOT EXISTS '+MACRO_TABLE+' ('
        'macro_ref TEXT PRIMARY KEY,authorization_id TEXT UNIQUE NOT NULL,authorization_hash TEXT NOT NULL)')
    for op in ('UPDATE','DELETE'):
        cur.execute('CREATE TRIGGER IF NOT EXISTS '+MACRO_TABLE+'_'+op.lower()+' BEFORE '+op+' ON '+MACRO_TABLE+
            " BEGIN SELECT RAISE(ABORT,'memo timeout slots are append-only'); END")
    cur.execute('CREATE TRIGGER IF NOT EXISTS '+MACRO_TABLE+'_insert BEFORE INSERT ON '+MACRO_TABLE+
        " WHEN dalton_scheduler_authorized()=0 BEGIN SELECT RAISE(ABORT,'Scheduler authority required'); END")
    cur.execute('INSERT OR IGNORE INTO '+MACRO_TABLE+' VALUES(?,?,?)',
                (scope['macro_ref'],record['id'],record['content_hash']))
    row=cur.execute('SELECT authorization_id FROM '+MACRO_TABLE+' WHERE macro_ref=?',(scope['macro_ref'],)).fetchone()
    return row['authorization_id'] == record['id']


def recover(wrapper, exc, kwargs):
    from .cockpit_model import model_failure_trace
    from .scheduler import Scheduler
    from .planner_paid_output_recovery import recovery_readers
    from .cockpit_timeout_recovery import failure_proof,reserve,saved_successor
    from .investment_memo_draft import GROUPS
    trace = model_failure_trace(exc)
    if (trace is None or kwargs['purpose'] != 'investment_memo' or not wrapper.stage
            or not wrapper.stage['allow_technical'] or wrapper.technical_slot_consumed):
        return None
    macro = getattr(wrapper, 'macro', None)
    if macro is not None and (not macro.complete or (getattr(macro, 'extra', None) or {}).get('role') in {'semantic','legacy_exhausted'}):
        return None
    with Scheduler(wrapper.scheduler_db) as scheduler, recovery_readers(
            wrapper.config['model_router_db'],wrapper.config['budget_db'],now=wrapper.clock()) as (router,budget):
        proof = failure_proof(scheduler.connection,router,budget,
                              work_ref=trace['work_order_ref'],mission=kwargs['mission'])
        if proof is None:
            return None
        company,group,index,material_hash=prompt_identity(kwargs['prompt'])
        scope={'contract_ref':CONTRACT_REF,'macro_ref':'memo-timeout-macro:'+content_hash({
            'company_ref':company['company_ref'],'mission_ref':kwargs['mission']['mission_ref'],
            'material_hash':material_hash}),'company_ref':company['company_ref'],'group':group,
            'group_index':index,'material_hash':material_hash,'max_units':wrapper.stage['max_units'],
            'max_repairs':1,'run_micros':wrapper.run_micros,'producer_micros':wrapper.producer_micros,
            'verifier_micros':wrapper.verifier_micros,'completed':list(wrapper.completed_calls),
            'accounted_micros':wrapper.accounted_micros}
        scope['completed_proofs']=[_successful(scheduler.connection,router,budget,call,kwargs['mission'],
            group=GROUPS[i][0],
            company_ref=company['company_ref'],material_hash=material_hash)
            for i,call in enumerate(scope['completed'])]
        # The wrapper may have conservatively accounted a still higher bill.
        history=historical_costs(scheduler.connection,budget,[c['work_order_ref']for c in scope['completed']]+[proof['parent_work_ref']],mission_ref=kwargs['mission']['mission_ref'],excluded_timeout_parent=proof['parent_work_ref'])
        scope['historical_costs']=history
        scope['accounted_micros']=max(sum(item['accounted_micros']for item in history),
            sum(p['accounted_micros'] for p in scope['completed_proofs'])+max(wrapper.producer_micros,proof['accounted_micros']))
        wrapper.accounted_micros=max(wrapper.accounted_micros,scope['accounted_micros'])
        if (scope['accounted_micros'] != wrapper.accounted_micros
                or scope['accounted_micros']+(len(GROUPS)-index)*wrapper.producer_micros+wrapper.verifier_micros > wrapper.run_micros):
            return None
        record=reserve(scheduler,router,budget,work=WorkOrder.from_dict(
            scheduler.work_order_authority(proof['parent_work_ref'])['work_order']),
            mission=kwargs['mission'],config=wrapper.config,now=wrapper.clock(),memo_scope=scope)
        if record is None:
            return None
        if getattr(wrapper, 'macro', None) is not None:
            try:
                wrapper.macro.claim_technical(proof['parent_work_ref'])
            except ValueError:
                return None
        original=record['memo_scope']
        if wrapper.accounted_micros+(len(GROUPS)-index)*wrapper.producer_micros+wrapper.verifier_micros > min(wrapper.run_micros,original['run_micros']):
            return None
        wrapper.technical_slot_consumed=True
        answered=saved_successor(scheduler,record)
        if answered is not None:
            work,formal=answered
            return wrapper.model._answer(formal,work,True,0,'replayed')
    if wrapper.stage.get('quiesce') is not None:
        wrapper.stage['quiesce'].checkpoint()
    try:
        return wrapper.model.call(**{**kwargs,'request_id':proof['parent_request_id']+record['suffix'],
                                 '_timeout_recovery':record})
    except BaseException:
        wrapper.accounted_micros += wrapper.producer_micros
        raise
