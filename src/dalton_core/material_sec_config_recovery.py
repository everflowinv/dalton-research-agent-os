"""Once-per-effective-config recovery of exact local SEC no-spec refusals.

This appends scheduling history only. It never sends, refunds, settles, or
provides permission beyond the ordinary governed source launcher.
"""
from contextlib import closing
from datetime import datetime, timedelta, timezone
from pathlib import Path
import json
import sqlite3
import subprocess
import time
from .store import canonical_json, content_hash
from .coverage_mission import CoverageMissionError
from .material_source_queue_recovery import QueueRecoveryIneligible, _configured_sec

RULE_REF = 'material-sec-local-config-refusal-recovery:0.1'


def _unsent(state, query_ref, spec_ref, connection):
    rows=connection.execute('SELECT * FROM material_targeted_query_events WHERE query_id=? ORDER BY event_id',(query_ref,)).fetchall()
    refused=[]
    for row in rows:
        body=json.loads(row['record_json'])
        if (canonical_json(body)!=row['record_json'] or body.get('status')!=row['status']
                or set(body)!={'status','kind','reason','dispatch_ref','ticket_ref'}
                or body['dispatch_ref'] is not None or body['ticket_ref'] is not None):
            raise QueueRecoveryIneligible('targeted_event_delivery_not_proved')
        if row['status']=='refused' and body['kind']=='no_spec' and body['reason']=='plan has no spec '+spec_ref:
            refused.append({'event_id':row['event_id'],'content_hash':content_hash(body),'created_at':row['created_at']})
        elif row['status']=='deferred' and body['kind']=='busy':pass
        elif row['status']=='refused' and body['kind']=='request_binding' and body['reason']=='material request is no longer active':pass
        else:raise QueueRecoveryIneligible('targeted_event_is_not_local_no_send')
    if not refused:raise QueueRecoveryIneligible('exact_no_spec_event_missing')
    # Launcher publishes the bound ticket before spawn. A durable ticket,
    # even one missing its event or already terminal, excludes this path.
    until=time.monotonic()+1
    for path in (Path(state)/'discoveries').glob('*/ticket.json'):
        if time.monotonic()>=until:raise QueueRecoveryIneligible('durable_ticket_scan_deadline')
        try:
            if path.is_symlink() or path.stat().st_size>2_000_000:raise ValueError('unsafe ticket')
            ticket=json.loads(path.read_text())
        except (OSError,ValueError):raise QueueRecoveryIneligible('durable_ticket_unreadable')
        if (ticket.get('targeted_binding')or{}).get('query_ref')==query_ref:
            raise QueueRecoveryIneligible('durable_targeted_ticket_exists')
    # Also reject an extant exact child if its ticket was lost. Unavailable
    # process evidence is not evidence of no delivery. No signal is sent.
    try:
        result=subprocess.run(['ps','-axo','command='],capture_output=True,text=True,timeout=1,check=True)
    except (OSError,subprocess.SubprocessError):raise QueueRecoveryIneligible('physical_query_scan_unavailable')
    if any(query_ref in line and '--targeted-binding' in line for line in result.stdout.splitlines()):
        raise QueueRecoveryIneligible('physical_targeted_child_exists')
    return refused[-1], rows[-1]['event_id']


def _current(state, view, query_ref, now):
    proof=_configured_sec(state,view,query_ref,now=now,allow_closed=True)
    from .macos_launchagent import _sec_discovery_plan
    from .mission_source_discovery import load_discovery_plan
    from .alphaengine_query_forms import effective_discovery_plan
    from .material_targeted_sec import targeted_sec_parameters
    from .readonly_sqlite import connect_read_only
    from .coverage_mission import CoverageMissionAuthority
    from .material_acquisition import TargetedSourceRung
    plan=effective_discovery_plan(load_discovery_plan(_sec_discovery_plan(Path(state))))
    if plan['content_hash']!=proof['effective_plan_hash']:raise QueueRecoveryIneligible('effective_plan_changed')
    parameters,binding=targeted_sec_parameters(state,plan=plan,company_ref=view['subject'],
        spec_ref=TargetedSourceRung('sec_edgar').spec_for(view),
        as_of=datetime.fromisoformat(now.replace('Z','+00:00')).date(),
        mission_version_ref=proof['mission_version_ref'],mission_version_hash=proof['mission_version_hash'],
        query_ref=query_ref,allow_closed=True)
    with closing(connect_read_only(Path(state)/'core.sqlite'))as c:
        c.row_factory=sqlite3.Row;c.execute('PRAGMA busy_timeout=100');c.execute('BEGIN')
        authority=object.__new__(CoverageMissionAuthority);authority.connection=c
        mission=authority.active_mission(plan['mission_ref'])
        if (mission['id'],mission['content_hash'])!=(proof['mission_version_ref'],proof['mission_version_hash']):
            raise QueueRecoveryIneligible('current_mission_changed')
        need=None
        if (view.get('acceptance')or{}).get('unit_proof_rule_ref'):
            from .material_model_unit_priority import unit_input_proof
            need=unit_input_proof(c,view,mission,contract_ref=RULE_REF,cache={})
            if need is None:raise QueueRecoveryIneligible('current_unit_need_not_proved')
    # Exact parameters, not wall time/mission-version churn, define the one
    # configuration opportunity. Current mission/grant remains a CAS guard.
    spec=next(s for s in plan['specs']if s['spec_ref']==binding['spec_ref'])
    epoch=content_hash({'rule_ref':RULE_REF,'spec_ref':binding['spec_ref'],
        'form':spec['form'],'lookback_days':spec['lookback_days'],
        'parameters':{k:v for k,v in parameters.items()if k not in ('date_from','date_to')},
        'explicit_dates':{k:view.get('acceptance',{}).get(k)for k in ('date_from','date_to')}})
    return {**proof,'effective_epoch':epoch,'parameters_hash':content_hash(parameters),'current_need':need}


def _candidate(state, view, *, now):
    from . import material_requests as mr,material_acquisition as ma
    from .readonly_sqlite import connect_read_only
    if (view.get('status') not in ('open','partial','exhausted','timeout')
            or view.get('closed') and view.get('status') not in ('exhausted','timeout')):return None
    # A live deadline is not silently extended. Closed local failures get a
    # normal new round only after their exact current need is reauthorized.
    if not view.get('closed') and view.get('deadline'):
        deadline=datetime.fromisoformat(view['deadline'].replace('Z','+00:00'))
        instant=datetime.fromisoformat(now.replace('Z','+00:00'))
        if deadline.tzinfo is None or instant.tzinfo is None or deadline.astimezone(timezone.utc)<instant.astimezone(timezone.utc):return None
    events=list(view.get('events')or[])
    attempts=[e for e in events if e.get('event_kind')=='attempt'and e.get('source')=='sec_edgar']
    if not attempts:return None
    last=attempts[-1]
    if last.get('outcome') not in ('dispatched','not_connected'):return None
    sent=next((e for e in reversed(attempts)if ma._targeted_query_id(e)),None)
    query_ref=ma._targeted_query_id(sent)
    if not query_ref:return None
    with closing(connect_read_only(mr.ledger_path(state)))as c:
        c.row_factory=sqlite3.Row;c.execute('PRAGMA busy_timeout=100');c.execute('BEGIN')
        row=c.execute('SELECT * FROM material_requests WHERE request_id=?',(view['id'],)).fetchone()
        if row is None:return None
        held=mr._view(c,row,with_events=True)
        if any(held.get(k)!=view.get(k)for k in ('events','status','closed','content_hash','subject','need','period','acceptance')):return None
        q=c.execute('SELECT * FROM material_targeted_queries WHERE query_id=?',(query_ref,)).fetchone()
        if q is None or q['request_id']!=view['id']or q['source_ref']!='source:sec-edgar':return None
        # targeted_sec_parameters below verifies canonical bytes, projected
        # columns and the digest-derived query/request identities.
        no_send,last_query_event=_unsent(state,query_ref,q['spec_ref'],c)
        if last.get('outcome')=='not_connected' and last.get('detail')!='plan has no spec '+q['spec_ref']:return None
    configured=_current(state,view,query_ref,now)
    key=content_hash({'rule_ref':RULE_REF,'request_ref':view['id'],'epoch':configured['effective_epoch']})
    if any((e.get('source_queue_recovery')or{}).get('recovery_key')==key for e in events):return None
    parent=max((i for i,e in enumerate(events)if e.get('event_kind')=='opened'),default=-1)
    if parent<0:return None
    body={'rule_ref':RULE_REF,'source':'sec_edgar','recovery_key':key,
        'request_ref':view['id'],'request_hash':view['content_hash'],
        'parent_open_event_id':events[parent]['event_id'],'parent_last_event_id':events[-1]['event_id'],
        'parent_query_ref':query_ref,'parent_query_last_event_id':last_query_event,
        'local_no_send':no_send,'configured':configured}
    return {**body,'content_hash':content_hash(body)}


def qualification(state, view, *, now):
    try:
        proof=_candidate(state,view,now=now)
        return {'status':'eligible','proof':proof}if proof else{'status':'ineligible','reason':'local_config_refusal_not_proved'}
    except QueueRecoveryIneligible as exc:return {'status':'ineligible','reason':str(exc)}
    except (OSError,ValueError,KeyError,TypeError,sqlite3.Error,CoverageMissionError)as exc:return {'status':'unverified','reason':type(exc).__name__}


def recover(state, view, *, now):
    from . import material_requests as mr
    proof=qualification(state,view,now=now).get('proof')
    if proof is None:return None
    with mr._writer(state)as c:
        row=c.execute('SELECT * FROM material_requests WHERE request_id=?',(view['id'],)).fetchone()
        if row is None or row['content_hash']!=proof['request_hash']:return None
        current=mr._view(c,row,with_events=True)
        if (current['events'][-1]['event_id']!=proof['parent_last_event_id']
                or any((e.get('source_queue_recovery')or{}).get('recovery_key')==proof['recovery_key']for e in current['events'])):return None
        # Revalidate current authority/configuration and no-send evidence at
        # the scheduling CAS. A subsequent dispatch rechecks them again.
        try:
            if _current(state,current,proof['parent_query_ref'],now)!=proof['configured']:return None
            q=c.execute('SELECT spec_ref FROM material_targeted_queries WHERE query_id=?',(proof['parent_query_ref'],)).fetchone()
            nosend,last=_unsent(state,proof['parent_query_ref'],q['spec_ref'],c)
        except (OSError,ValueError,KeyError,TypeError,sqlite3.Error,CoverageMissionError):return None
        if nosend!=proof['local_no_send']or last!=proof['parent_query_last_event_id']:return None
        deadline=(datetime.fromisoformat(now.replace('Z','+00:00'))+timedelta(hours=mr.DEFAULT_DEADLINE_HOURS)).isoformat(timespec='seconds')if current['closed']else current['deadline']
        body={'reopened':True,'reason':'SEC 检索规则已更新，重新核验授权后正常接回本地未发送步骤',
              'deadline':deadline,'previous_status':current['status'],'source_queue_recovery':proof}
        c.execute('INSERT INTO material_request_events(request_id,event_kind,status,requester_json,record_json,actor_ref,created_at) VALUES(?,?,?,?,?,?,?)',
            (view['id'],'opened','open',None,mr._canonical(body),'automation:material-acquisition',now))
    return proof
