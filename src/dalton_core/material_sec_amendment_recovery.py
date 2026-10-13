"""One ordinary reentry after an exact, durably accounted index parser failure.

No delivery/fee is erased. This is not a no-send claim and grants no new source
permission or budget. The normal launcher must recheck both before dispatch.
"""
from contextlib import closing
from datetime import datetime, timedelta
import json
from pathlib import Path
import re
import sqlite3

from .store import canonical_json, content_hash
from .readonly_sqlite import connect_read_only

RULE_REF = 'material-sec-independent-amendment-recovery:0.1'
ERROR = {'code':'normalization_error','message':'SEC amendment lacks explicit revision authority','retryable':False}


def _record(connection, table, column, ref):
    row=connection.execute('SELECT * FROM '+table+' WHERE '+column+'=?',(ref,)).fetchone()
    if row is None:raise ValueError('amendment recovery authority missing')
    body=json.loads(row['record_json'])
    if (body.get('id')!=ref or canonical_json(body)!=row['record_json'] or body.get('content_hash')!=row['content_hash']
            or content_hash({k:v for k,v in body.items()if k!='content_hash'})!=row['content_hash']):
        raise ValueError('amendment recovery authority hash differs')
    for key in row.keys():
        if key in body and not isinstance(body[key],(dict,list)) and row[key]!=body[key]:
            raise ValueError('amendment recovery SQL binding differs')
    if 'actual_json' in row.keys() and canonical_json(body.get('actual'))!=row['actual_json']:
        raise ValueError('amendment recovery actual quantity projection differs')
    return body


def _json_file(path):
    if path.is_symlink() or not path.is_file() or path.stat().st_size>2_000_000:
        raise ValueError('amendment recovery ticket unavailable')
    raw=path.read_bytes();return json.loads(raw),__import__('hashlib').sha256(raw).hexdigest()


def _failure(state, ledger, core, query_ref, view):
    from .runner_journal import RunnerJournal
    from .connector_runner import validate_connector_runner_request,validate_connector_runner_response
    from .contracts import ResultEnvelope,WorkOrder
    q=ledger.execute('SELECT * FROM material_targeted_queries WHERE query_id=?',(query_ref,)).fetchone()
    if q is None or q['request_id']!=view['id']:raise ValueError('amendment recovery query differs')
    row=ledger.execute('SELECT * FROM material_targeted_query_events WHERE query_id=? ORDER BY event_id DESC LIMIT 1',(query_ref,)).fetchone()
    if row is None:raise ValueError('amendment recovery dispatch missing')
    event=json.loads(row['record_json'])
    if canonical_json(event)!=row['record_json'] or event.get('status')!=row['status'] or event['status']!='launched':
        raise ValueError('amendment recovery dispatch is not final-bound')
    ticket_ref=event.get('ticket_ref');dispatch_ref=event.get('dispatch_ref')
    if not isinstance(ticket_ref,str) or re.fullmatch(r'sec-filings-index:[a-f0-9]{24}',ticket_ref) is None:
        raise ValueError('amendment recovery ticket identity invalid')
    directory=Path(state)/'discoveries'/ticket_ref.split(':')[1]
    ticket,ticket_hash=_json_file(directory/'ticket.json');summary,summary_hash=_json_file(directory/'summary.json')
    binding=ticket.get('targeted_binding')or{}
    if (ticket.get('id')!=ticket_ref or ticket.get('status')!='failed' or ticket.get('exit_code')!=1
            or summary.get('status')!='failed' or summary.get('failure_reason')!='index outcome failed'
            or summary.get('targeted_binding')!=binding or event.get('targeted_binding')!=binding
            or binding.get('request_ref')!=view['id'] or binding.get('request_hash')!=view['content_hash']
            or binding.get('query_ref')!=query_ref or binding.get('company_ref')!=view['subject']):
        raise ValueError('amendment recovery ticket/request binding differs')
    dispatch_row=core.execute('SELECT * FROM coverage_mission_discovery_dispatches WHERE dispatch_id=?',(dispatch_ref,)).fetchone()
    if dispatch_row is None:raise ValueError('amendment recovery dispatch missing')
    dispatch=dict(dispatch_row)
    if any(dispatch.get(k)!=v for k,v in {'ticket_ref':ticket_ref,'company_ref':view['subject'],
            'mission_version_ref':binding['mission_version_ref'],'discovery_plan_ref':binding['plan_ref'],
            'discovery_plan_hash':binding['plan_hash'],'spec_ref':binding['spec_ref'],'status':'failed'}.items()):
        raise ValueError('amendment recovery original dispatch differs')
    index=summary['index'];inv=_record(core,'connector_invocations','connector_invocation_id',index['connector_invocation_ref'])
    call=_record(core,'connector_call_specs','call_spec_id',inv['call_spec_ref'])
    if (inv['content_hash']!=index['connector_invocation_hash'] or call['content_hash']!=inv['call_spec_hash']
            or call['operation']!='list_filings' or call['parameters']!=summary['parameters']
            or call['parameters']['issuer']!=view['acceptance']['cik']
            or call['parameters']['date_from']!=view['acceptance']['date_from']
            or call['parameters']['date_to']!=view['acceptance']['date_to']):
        raise ValueError('amendment recovery invocation scope differs')
    rows=core.execute('SELECT * FROM runner_request_journal WHERE connector_invocation_ref=? LIMIT 2',(inv['id'],)).fetchall()
    if len(rows)!=1:raise ValueError('amendment recovery runner identity ambiguous')
    rr=rows[0];request=validate_connector_runner_request(json.loads(rr['request_json']))
    if canonical_json(request)!=rr['request_json'] or request['content_hash']!=rr['request_hash']:
        raise ValueError('amendment recovery runner hash differs')
    if (request['connector_invocation_hash']!=inv['content_hash'] or request['call_spec_hash']!=call['content_hash']
            or request['connector_invocation_ref']!=inv['id'] or request['call_spec_ref']!=call['id']
            or request['work_order_ref']!=inv['work_order_ref'] or request['work_order_hash']!=inv['work_order_hash']):
        raise ValueError('amendment recovery runner binding differs')
    journal=object.__new__(RunnerJournal);journal._connection=core
    event_rows=core.execute('SELECT * FROM runner_attempt_journal_events WHERE runner_request_ref=? ORDER BY event_seq LIMIT 65',(rr['runner_request_ref'],)).fetchall()
    history=[journal._event(row,n)for n,row in enumerate(event_rows,1)]
    if not history or len(history)>64 or history[-1]['state']!='responded':raise ValueError('amendment recovery runner not terminal')
    for e in history:
        base={k:e[k]for k in ('runner_request_ref','request_ordinal','state','reservation_ref','event_at','recorded_at','payload')}
        if (content_hash(base)!=e['content_hash'] or e['id']!='runner-journal-event:'+content_hash(
                {'runner_request_ref':e['runner_request_ref'],'request_ordinal':e['request_ordinal'],'content_hash':e['content_hash']})):
            raise ValueError('amendment recovery runner event hash differs')
    response=validate_connector_runner_response(history[-1]['payload']['response'])
    observed=[e for e in history if e['state']=='observed']
    if len(observed)!=1:raise ValueError('amendment recovery physical attempts ambiguous')
    from .connector_runner import validate_adapter_transport_observation
    observed=observed[0];observation=validate_adapter_transport_observation(observed['payload']['observation'])
    if (response['id']!=index['runner_response_ref'] or response['runner_request_ref']!=request['id']
            or response['runner_request_hash']!=request['content_hash'] or response['connector_invocation_ref']!=inv['id']
            or response['connector_invocation_hash']!=inv['content_hash'] or response['outcome']!='failed'
            or observed['payload']['attempt_outcome']!='failed' or observed['payload']['error']!=ERROR
            or observation['error']!=ERROR or observation['provider_status_code']!=200):
        raise ValueError('amendment recovery exact normalization failure not proved')
    proof={}
    for prefix,table,key in [('physical_attempt','connector_physical_attempts','physical_attempt_id'),
            ('usage_entry','connector_usage_entries','usage_entry_id'),('cost_entry','connector_cost_entries','cost_entry_id'),
            ('quota_settlement','connector_quota_settlements','settlement_id')]:
        body=_record(core,table,key,response[prefix+'_ref'])
        if body['content_hash']!=response[prefix+'_hash']:raise ValueError('amendment recovery accounting binding differs')
        if body['id']!=response[prefix+'_ref']:raise ValueError('amendment recovery accounting record identity differs')
        if prefix=='physical_attempt' and (body['connector_invocation_ref']!=inv['id'] or body['reservation_ref']!=history[-1]['reservation_ref']
                or body['outcome']!='failed' or body.get('error')!=ERROR):raise ValueError('amendment recovery physical failure differs')
        if prefix=='usage_entry' and (body['connector_invocation_ref']!=inv['id'] or body['physical_attempt_ref']!=response['physical_attempt_ref']):raise ValueError('amendment recovery usage differs')
        if prefix=='cost_entry' and body['usage_entry_ref']!=response['usage_entry_ref']:raise ValueError('amendment recovery cost differs')
        proof[prefix+'_ref']=body['id'];proof[prefix+'_hash']=body['content_hash']
        if prefix=='quota_settlement':
            if (body['state'] not in ('consumed','indeterminate') or body['reservation_ref']!=history[-1]['reservation_ref']
                    or body['usage_entry_ref']!=response['usage_entry_ref'] or body['cost_entry_ref']!=response['cost_entry_ref']):
                raise ValueError('amendment recovery accounting binding differs')
            proof['settlement_state']=body['state']
            reservation=_record(core,'connector_quota_reservations','reservation_id',body['reservation_ref'])
            if reservation['connector_invocation_ref']!=inv['id']:raise ValueError('amendment recovery reservation differs')
            proof['reservation_ref']=reservation['id'];proof['reservation_hash']=reservation['content_hash']
    with closing(connect_read_only(Path(state)/'scheduler.sqlite'))as scheduler:
        scheduler.row_factory=sqlite3.Row;scheduler.execute('PRAGMA busy_timeout=100');scheduler.execute('BEGIN')
        r=scheduler.execute('SELECT * FROM scheduler_result_envelopes WHERE result_envelope_id=?',(response['result_envelope_ref'],)).fetchone()
        w=scheduler.execute('SELECT * FROM scheduler_work_orders WHERE work_order_id=?',(inv['work_order_ref'],)).fetchone()
        if r is None or w is None:raise ValueError('amendment recovery formal result missing')
        receipt={k:r[k]for k in ('result_envelope_id','work_order_id','attempt_number','result_envelope_hash','outcome','created_at')}
        if content_hash(receipt)!=r['content_hash']:raise ValueError('amendment recovery result receipt hash differs')
        work=WorkOrder.from_dict(json.loads(w['work_order_json']));result=ResultEnvelope.from_dict(json.loads(r['result_envelope_json']))
        if (canonical_json(work.to_dict())!=w['work_order_json'] or content_hash(work.to_dict())!=w['work_order_hash']
                or w['work_order_hash']!=inv['work_order_hash'] or work.id!=inv['work_order_ref']
                or canonical_json(result.to_dict())!=r['result_envelope_json'] or content_hash(result.to_dict())!=r['result_envelope_hash']
                or r['result_envelope_hash']!=response['result_envelope_hash'] or r['work_order_id']!=work.id
                or r['attempt_number']!=request['scheduler_attempt_number'] or result.work_order_ref!=work.id
                or result.to_dict()['status']!='failed' or result.invocation_ref!=inv['execution_ref']):
            raise ValueError('amendment recovery formal binding differs')
    return {**proof,'query_ref':query_ref,'query_last_event_id':row['event_id'],'dispatch_ref':dispatch_ref,
        'dispatch_snapshot_hash':content_hash(dispatch),'ticket_ref':ticket_ref,'ticket_sha256':ticket_hash,'summary_sha256':summary_hash,
        'invocation_ref':inv['id'],'invocation_hash':inv['content_hash'],'runner_request_ref':request['id'],
        'runner_request_hash':request['content_hash'],'responded_event_ref':history[-1]['id'],
        'responded_event_hash':history[-1]['content_hash'],'result_ref':result.id,'result_hash':response['result_envelope_hash'],
        'error':ERROR}


def _candidate(state,view,now):
    from . import material_requests as mr,material_acquisition as ma
    from .material_source_queue_recovery import _configured_sec
    from .coverage_mission import CoverageMissionAuthority
    from .material_model_unit_priority import unit_input_proof
    if view.get('status') not in ('open','partial','exhausted','timeout'):return None
    key=content_hash({'rule_ref':RULE_REF,'request_ref':view['id']})
    events=view.get('events')or[]
    if any((e.get('source_queue_recovery')or{}).get('recovery_key')==key for e in events):return None
    attempts=ma._round_attempts(view)
    sent=next((e for e in reversed(attempts)if e.get('source')=='sec_edgar' and ma._targeted_query_id(e)),None)
    if sent is None:return None
    instant=datetime.fromisoformat(now.replace('Z','+00:00'))
    if instant.tzinfo is None:raise ValueError('amendment recovery current time is not zoned')
    if not view.get('closed'):
        if not view.get('deadline'):return None
        end=datetime.fromisoformat(view['deadline'].replace('Z','+00:00'))
        if end.tzinfo is None:raise ValueError('amendment recovery deadline is not zoned')
        if end<=instant:return None
    # This recovery serves a still-current exact filed-unit prerequisite,
    # not an arbitrary requester's label or a stale historical source error.
    if not (view.get('acceptance')or{}).get('unit_proof_rule_ref'):return None
    query_ref=ma._targeted_query_id(sent)
    with closing(connect_read_only(mr.ledger_path(state)))as ledger,closing(connect_read_only(Path(state)/'core.sqlite'))as core:
        for c in (ledger,core):c.row_factory=sqlite3.Row;c.execute('PRAGMA busy_timeout=100');c.execute('BEGIN')
        row=ledger.execute('SELECT * FROM material_requests WHERE request_id=?',(view['id'],)).fetchone()
        if row is None:return None
        current=mr._view(ledger,row,with_events=True)
        if current!=view:return None
        # Read the small exact failed-call chain first. An unrelated failure
        # must not force replay of a large financial context on every tick.
        failure=_failure(state,ledger,core,query_ref,view)
        configured=_configured_sec(state,view,query_ref,now=now,allow_closed=True)
        authority=object.__new__(CoverageMissionAuthority);authority.connection=core
        mission=authority.mission(configured['mission_version_ref'])
        need=unit_input_proof(core,current,mission,contract_ref=RULE_REF,cache={})
        if need is None:return None
    parent=max(i for i,e in enumerate(events)if e['event_kind']=='opened')
    body={'rule_ref':RULE_REF,'source':'sec_edgar','recovery_key':key,'request_ref':view['id'],'request_hash':view['content_hash'],
        'parent_open_event_id':events[parent]['event_id'],'parent_last_event_id':events[-1]['event_id'],
        'parent_query_ref':query_ref,'configured':configured,'current_need':need,'failure':failure}
    return {**body,'content_hash':content_hash(body)}


def qualification(state,view,*,now):
    try:
        p=_candidate(state,view,now);return {'status':'eligible','proof':p}if p else {'status':'ineligible','reason':'exact_current_amendment_failure_not_proved'}
    except Exception as exc:
        return {'status':'unverified','reason':type(exc).__name__}


def recover(state,view,*,now):
    from . import material_requests as mr
    proof=qualification(state,view,now=now).get('proof')
    if proof is None:return None
    with mr._writer(state)as c:
        row=c.execute('SELECT * FROM material_requests WHERE request_id=?',(view['id'],)).fetchone()
        current=mr._view(c,row,with_events=True)if row else None
        if current is None or current!=view:return None
        # Exact current mission/config, full failure chain, newest event and
        # once key are all rechecked while holding the scheduling CAS.
        if qualification(state,current,now=now).get('proof')!=proof:return None
        deadline=(datetime.fromisoformat(now.replace('Z','+00:00'))+timedelta(hours=mr.DEFAULT_DEADLINE_HOURS)).isoformat(timespec='seconds')if current['closed']else current['deadline']
        body={'reopened':True,'reason':'SEC 原表单独立发现规则已修正；保留原获取费用，按当前授权继续同一原件请求',
            'deadline':deadline,'previous_status':current['status'],'source_queue_recovery':proof}
        c.execute('INSERT INTO material_request_events(request_id,event_kind,status,requester_json,record_json,actor_ref,created_at) VALUES(?,?,?,?,?,?,?)',
            (view['id'],'opened','open',None,mr._canonical(body),'automation:material-acquisition',now))
    return proof
