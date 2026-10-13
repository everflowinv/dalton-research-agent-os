"""Read a finished retryable attempt without calling it settled or in flight."""
import json
from datetime import datetime
from decimal import Decimal
from .store import content_hash,canonical_json
from .scheduler import Scheduler
from .contracts import ResultEnvelope

CONTRACT_REF='investment-memo-existing-scheduler-retry:0.1'
MAX_EVENTS=128
MAX_ATTEMPTS=16


def _event(row):
    body={'schema_version':'0.1','id':row['event_id'],'created_at':row['created_at'],
        'work_order_ref':row['work_order_id'],'attempt_number':row['attempt_number'],
        'state':row['state'],'lease_ref':row['lease_revision_id'],
        'result_envelope_ref':row['result_envelope_id'],'result_envelope_hash':row['result_envelope_hash'],
        'reason':row['reason'],'prior_event_ref':row['prior_event_id']}
    if row['wire_version']=='0.2':body.update(wire_version='0.2',not_before=row['not_before'])
    elif row['wire_version'] is not None or row['not_before'] is not None:raise ValueError('memo retry attempt wire is unknown')
    if content_hash(body)!=row['content_hash']:raise ValueError('memo retry attempt hash differs')
    return body


def _lease(connection,ref,initial,work_ref,attempt):
    seen=set();previous=None
    for _ in range(64):
        if ref in seen:raise ValueError('memo retry lease chain repeats')
        seen.add(ref)
        row=connection.execute('SELECT * FROM scheduler_leases WHERE lease_revision_id=?',(ref,)).fetchone()
        if row is None:raise ValueError('memo retry lease is missing')
        body={'schema_version':'0.1','id':row['lease_revision_id'],'lease_id':row['lease_id'],
            'lease_version':row['lease_version'],'created_at':row['created_at'],'work_order_ref':row['work_order_id'],
            'attempt_number':row['attempt_number'],'owner_ref':row['owner_ref'],'lease_token_hash':row['lease_token_hash'],
            'issued_at':row['issued_at'],'renewed_at':row['renewed_at'],'expires_at':row['expires_at'],
            'prior_lease_ref':row['prior_lease_revision_id']}
        if (content_hash(body)!=row['content_hash'] or row['work_order_id']!=work_ref or row['attempt_number']!=attempt):
            raise ValueError('memo retry lease authority differs')
        if previous and (row['lease_id']!=previous['lease_id'] or row['lease_version']!=previous['lease_version']-1
                or row['owner_ref']!=previous['owner_ref'] or row['lease_token_hash']!=previous['lease_token_hash']):
            raise ValueError('memo retry lease ancestry differs')
        if ref==initial:return
        previous=row;ref=row['prior_lease_revision_id']
        if ref is None:break
    raise ValueError('memo retry lease chain does not reach its claim')


def prove_ready_retry(connection, work_ref, *, mission, now, _terminal=False):
    """No writes; every retryable receipt and the current ready event are proved."""
    reader=Scheduler.__new__(Scheduler);reader.connection=connection
    authority=reader.work_order_authority(work_ref)
    formal=reader.formal_result(work_ref)
    if authority is None or (_terminal != (formal is not None)):raise ValueError('memo retry terminal authority differs')
    work=authority['work_order'];metadata=work['metadata']
    if (metadata.get('purpose') not in {'investment_memo','investment_memo_verifier'}
            or metadata.get('control_plane')!='cockpit'
            or (metadata.get('mission_version_ref'),metadata.get('mission_version_hash'))!=(mission['id'],mission['content_hash'])):
        raise ValueError('memo retry current mission/purpose differs')
    frozen=connection.execute('SELECT policy_version_id,max_attempts FROM scheduler_work_orders WHERE work_order_id=?',(work_ref,)).fetchone()
    if frozen is None:raise ValueError('memo retry frozen Work is missing')
    policy_row=connection.execute('SELECT * FROM scheduler_policy_versions WHERE policy_version_id=?',(frozen['policy_version_id'],)).fetchone()
    if frozen is None or policy_row is None:raise ValueError('memo retry frozen policy is missing')
    policy=json.loads(policy_row['policy_json'])
    if (canonical_json(policy)!=policy_row['policy_json'] or content_hash({k:v for k,v in policy.items()if k!='created_at'})!=policy_row['policy_hash']
            or policy['id']!=frozen['policy_version_id'] or policy['max_attempts']!=frozen['max_attempts']):
        raise ValueError('memo retry frozen attempt policy differs')
    rows=connection.execute('SELECT * FROM scheduler_attempt_events WHERE work_order_id=? ORDER BY event_seq LIMIT ?',(work_ref,MAX_EVENTS+1)).fetchall()
    if not rows or len(rows)>MAX_EVENTS:raise ValueError('memo retry attempt history exceeds bound')
    prior=None;by_attempt={}
    for row in rows:
        _event(row)
        if row['prior_event_id']!=prior:raise ValueError('memo retry attempt ancestry differs')
        prior=row['event_id'];by_attempt.setdefault(row['attempt_number'],[]).append(row)
    latest=rows[-1];number=latest['attempt_number']
    final_state=formal['terminal_state'] if _terminal else 'ready'
    expected_tails=([['ready','leased',final_state],['ready','leased','retryable','failed']]
        if _terminal and final_state=='failed' else [['ready','leased',final_state]] if _terminal else [['ready']])
    if (latest['state']!=final_state or number<2 or number>min(MAX_ATTEMPTS,frozen['max_attempts'])
            or sorted(by_attempt)!=list(range(1,number+1))
            or [e['state']for e in by_attempt[number]] not in expected_tails
            or (not _terminal and (latest['lease_revision_id'] is not None or latest['result_envelope_id'] is not None))):
        raise ValueError('memo retry is not a completed attempt followed by a remaining ready attempt')
    if not _terminal and latest['not_before'] is not None and datetime.fromisoformat(latest['not_before'])>now:
        raise ValueError('memo retry is not due')
    if connection.execute('SELECT 1 FROM scheduler_current_leases WHERE work_order_id=?',(work_ref,)).fetchone():
        raise ValueError('memo retry still has a current lease')
    results=[]
    for attempt in range(1,number):
        events=by_attempt[attempt]
        if [e['state']for e in events]!=['ready','leased','retryable']:raise ValueError('memo retry prior attempt is not completed retryable')
        finished=events[-1]
        _lease(connection,finished['lease_revision_id'],events[1]['lease_revision_id'],work_ref,attempt)
        row=connection.execute('SELECT * FROM scheduler_result_envelopes WHERE result_envelope_id=?',(finished['result_envelope_id'],)).fetchone()
        if row is None:raise ValueError('memo retry result receipt is missing')
        raw=json.loads(row['result_envelope_json']);normalized=ResultEnvelope.from_dict(raw).to_dict()
        receipt={k:row[k]for k in ('result_envelope_id','work_order_id','attempt_number','result_envelope_hash','outcome','created_at')}
        if (raw!=normalized or canonical_json(raw)!=row['result_envelope_json'] or content_hash(raw)!=row['result_envelope_hash']
                or content_hash(receipt)!=row['content_hash'] or row['work_order_id']!=work_ref or row['attempt_number']!=attempt
                or row['outcome']!='retryable' or raw['status']!='retryable' or raw['work_order_ref']!=work_ref
                or raw['id']!=row['result_envelope_id']
                or row['result_envelope_hash']!=finished['result_envelope_hash']):
            raise ValueError('memo retry completed result differs')
        results.append({'result_ref':row['result_envelope_id'],'result_hash':row['result_envelope_hash'],
                        'attempt_number':attempt,'dispatch_state':raw.get('metadata',{}).get('dispatch_state')})
    reservation=int(Decimal(str(work['budget']['max_cost_usd']))*1_000_000)
    return {'contract_ref':CONTRACT_REF,'work_order_ref':work_ref,'work_order_hash':authority['work_order_hash'],
        'request_id':metadata['request_id'],'purpose':metadata['purpose'],'prompt_hash':content_hash(work['question']),
        'next_attempt':number,'max_attempts':frozen['max_attempts'],'ready_event_ref':latest['event_id'],
        'ready_event_hash':latest['content_hash'],'completed_results':results,
        'accounted_micros':reservation*(number-1),'unknown_fee_retained':True,
        'fee_certainty':'conservative_upper_bound','settled':False}


def retry_bill(connection,router,budget,authority,proof, *, terminal_bill=None):
    """Preserve all actual fees plus each unproved completed-attempt ceiling."""
    work=authority['work_order'];ref=work['id'];ceiling=int(Decimal(str(work['budget']['max_cost_usd']))*1_000_000)
    rows=budget.connection.execute('SELECT attempt_number,phase FROM thesis_impact_day_admissions WHERE work_order_ref=? ORDER BY attempt_number,phase LIMIT ?',
        (ref,2*MAX_ATTEMPTS+1)).fetchall()
    if len(rows)>2*MAX_ATTEMPTS:raise ValueError('memo retry fee history exceeds bound')
    previous={r['attempt_number']:r for r in proof['completed_results']}
    amounts={};fees=[]
    for row in rows:
        number=row['attempt_number']
        if terminal_bill is None and number not in previous:raise ValueError('memo retry has an unproved current/future admission')
        paid=budget.admission(work_order_ref=ref,attempt_number=number,phase=row['phase'])
        admission,settlement=paid['admission'],paid['settlement'];route=router.get_decision(admission['route_decision_ref']);mission=paid['mission_binding']or{}
        if (route['work_order_ref']!=ref or route['work_order_hash']!=authority['work_order_hash'] or route['attempt_number']!=number
                or (mission.get('mission_version_ref'),mission.get('mission_version_hash'))!=(work['metadata']['mission_version_ref'],work['metadata']['mission_version_hash'])):
            raise ValueError('memo retry fee source/mission differs')
        amount=admission['reserved_micros'] if settlement is None else settlement['actual_micros']
        if type(amount)is not int or amount<0:raise ValueError('memo retry fee amount is invalid')
        amounts[number]=amounts.get(number,0)+amount
        fees.append({'admission_ref':admission['admission_id'],'admission_hash':admission['content_hash'],
            'reserved_micros':admission['reserved_micros'],'settlement_ref':None if settlement is None else settlement['settlement_id'],
            'settlement_hash':None if settlement is None else settlement['content_hash']})
    if terminal_bill is None:
        total=sum(max(ceiling,amounts.get(number,0))for number in previous)
        return {'work_ref':ref,'work_hash':authority['work_order_hash'],'formal_hash':None,'fees':fees,
            'unknown_fee_retained':True,'accounted_micros':total,'scheduler_retry_proof':proof}
    extra=sum(max(0,ceiling-amounts.get(number,0))for number,r in previous.items()if r['dispatch_state'] not in {'not_started','definitely_not_sent','sent'})
    if extra:
        return {**terminal_bill,'accounted_micros':terminal_bill['accounted_micros']+extra,
            'unknown_fee_retained':True,'retained_retry_unknown_micros':extra,'scheduler_retry_proof':proof}
    return terminal_bill
