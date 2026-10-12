"""Source-bound recovery of a SEC request mistaken for an absent connector.

This is queue maintenance, not permission for a new paid research attempt.
The ordinary source launcher still validates the mission, request and budgets.
"""
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
import json
import sqlite3
import time
from .store import canonical_json, content_hash
from .coverage_mission import CoverageMissionError

RULE_REF = 'material-sec-queue-wait:0.1'
class QueueRecoveryIneligible(ValueError):
    pass

LEGACY_TIMEOUT_DETAIL = '3 小时内没有协调器执行这个检索（本环境可能未接入该来源）'


def _configured_sec(state, view, query_ref, *, now, allow_closed=False):
    """The exact plan used by this writer plus its current mission grant."""
    from .coverage_mission import CoverageMissionAuthority
    from .macos_launchagent import _sec_discovery_plan
    from .mission_source_discovery import load_discovery_plan
    from .alphaengine_query_forms import effective_discovery_plan
    from .material_targeted_sec import targeted_sec_parameters
    from .readonly_sqlite import connect_read_only
    plan_path = _sec_discovery_plan(Path(state))
    approved = load_discovery_plan(plan_path)
    plan = effective_discovery_plan(approved)
    with closing(connect_read_only(Path(state)/'core.sqlite')) as c:
        c.row_factory = sqlite3.Row
        c.execute('PRAGMA busy_timeout=100');c.execute('BEGIN')
        missions = object.__new__(CoverageMissionAuthority)
        missions.connection = c
        mission = missions.active_mission(plan['mission_ref'])
        if view['subject'] not in {x['company_ref'] for x in mission['universe']}:
            raise ValueError('company is not in the active dispatch universe')
        grant = missions.authorize_source_discovery(
            company_ref=view['subject'], source_ref='source:sec-edgar',
            requested_by=mission['autonomy']['automation_principal'],
            mission_version_ref=mission['id'], mission_version_hash=mission['content_hash'])
    from . import material_acquisition as ma
    spec = ma.TargetedSourceRung('sec_edgar').spec_for(view)
    _, binding = targeted_sec_parameters(state, plan=plan, company_ref=view['subject'],
        spec_ref=spec, as_of=datetime.fromisoformat(now.replace("Z","+00:00")).date(), mission_version_ref=mission['id'],
        mission_version_hash=mission['content_hash'], query_ref=query_ref, allow_closed=allow_closed)
    if binding['request_ref'] != view['id'] or binding['request_hash'] != view['content_hash']:
        raise ValueError('source queue request binding changed')
    return {'plan_ref':approved['id'],'plan_hash':approved['content_hash'],
            'effective_plan_hash':plan['content_hash'], 'mission_version_ref':mission['id'],
            'mission_version_hash':mission['content_hash'], 'grant_hash':content_hash(grant),
            'query_ref':query_ref,'query_hash':binding['query_hash'],
            'request_ref':view['id'],'request_hash':view['content_hash']}


def configured_wait(state, view, query_ref, *, now):
    if not query_ref:
        return None
    from .macos_launchagent import _sec_discovery_plan, SEC_PLAN_SELECTOR
    # Only a genuinely absent default and selector means no connector. An
    # unreadable/invalid selection is unverified, never evidence of absence.
    try:
        path = _sec_discovery_plan(Path(state))
        if not path.exists() and not (Path(state)/'discovery-plans'/SEC_PLAN_SELECTOR).exists():
            return None
        return {'status':'configured', 'proof':_configured_sec(state,view,query_ref,now=now)}
    except (OSError,ValueError,KeyError,TypeError,sqlite3.Error,CoverageMissionError):
        return {'status':'unverified'}


def _recovery_key(request_ref):
    return content_hash({'rule_ref':RULE_REF,'request_ref':request_ref,'source':'sec_edgar'})


def _candidate(state, view, *, now):
    """Pure, finite qualification; no generic transport failure is eligible."""
    from . import material_requests as mr, material_acquisition as ma
    if view.get('status') not in ('exhausted','timeout') or not view.get('closed'):
        return None
    events=list(view.get('events') or ())
    key=_recovery_key(view['id'])
    if any((e.get('source_queue_recovery') or {}).get('recovery_key')==key for e in events):
        return None
    last_open=max((i for i,e in enumerate(events)if e.get('event_kind')=='opened'),default=-1)
    attempts=[e for e in events[last_open+1:]if e.get('event_kind')=='attempt'and e.get('source')=='sec_edgar']
    if not attempts or attempts[-1].get('outcome')!='not_connected' or attempts[-1].get('detail')!=LEGACY_TIMEOUT_DETAIL:
        return None
    sent=next((e for e in reversed(attempts[:-1])if e.get('outcome')=='dispatched'),None)
    query_ref=ma._targeted_query_id(sent)
    if query_ref is None:return None
    from .readonly_sqlite import connect_read_only
    with closing(connect_read_only(mr.ledger_path(state))) as c:
        c.row_factory=sqlite3.Row;c.execute('PRAGMA busy_timeout=100');c.execute('BEGIN')
        original=c.execute('SELECT * FROM material_requests WHERE request_id=?',(view['id'],)).fetchone()
        if original is None:return None
        stored=mr._view(c,original,with_events=True)
        if any(stored.get(k)!=view.get(k) for k in ('events','status','closed','content_hash')):return None
        q=c.execute('SELECT * FROM material_targeted_queries WHERE query_id=?',(query_ref,)).fetchone()
        if q is None or q['request_id']!=view['id'] or q['source_ref']!='source:sec-edgar':return None
        rows=c.execute('SELECT * FROM material_targeted_query_events WHERE query_id=? ORDER BY event_id',(query_ref,)).fetchall()
        for row in rows:
            body=json.loads(row['record_json'])
            if canonical_json(body)!=row['record_json'] or body.get('status')!=row['status']:return None
            # An absent event is not a claim of no delivery. Refuse every known
            # launch/failure/credential or governance rejection, including one
            # written before the old close.
            if row['status'] in ('launched','failed') or body.get('ticket_ref') or body.get('dispatch_ref'):return None
            if row['status']=='refused' and (body.get('kind'),body.get('reason'))!=('request_binding','material request is no longer active'):return None
            if row['status']=='deferred' and body.get('kind') not in ('busy',):return None
    # A launcher can have durably started before it recorded a targeted event.
    # Existing exact tickets make this queue-only recovery ineligible.
    until=time.monotonic()+1
    for path in (Path(state)/'discoveries').glob('*/ticket.json'):
        if time.monotonic()>=until:raise QueueRecoveryIneligible('durable_ticket_scan_deadline')
        try:ticket=json.loads(path.read_text())
        except (OSError,ValueError):raise QueueRecoveryIneligible('durable_ticket_unreadable')
        if (ticket.get('targeted_binding') or {}).get('query_ref')==query_ref:
            raise QueueRecoveryIneligible('durable_targeted_ticket_exists')
    configured=_configured_sec(state,view,query_ref,now=now,allow_closed=True)
    proof={'rule_ref':RULE_REF,'recovery_key':key,'source':'sec_edgar',
        'request_ref':view['id'],'request_hash':view['content_hash'],
        'closed_event_id':events[-1]['event_id'],'parent_open_event_id':events[last_open]['event_id'],
        'parent_query_ref':query_ref,'configured':configured}
    proof['content_hash']=content_hash(proof)
    return proof


def qualification(state, view, *, now):
    """RO diagnostic; failure to prove eligibility is never reported as success."""
    try:
        proof = _candidate(state, view, now=now)
        return {'status':'eligible','proof':proof} if proof is not None else {'status':'ineligible','reason':'closed_queue_contract_not_proved'}
    except QueueRecoveryIneligible as exc:
        return {'status':'ineligible','reason':str(exc)}
    except (OSError,ValueError,KeyError,TypeError,sqlite3.Error,CoverageMissionError) as exc:
        return {'status':'unverified','reason':type(exc).__name__}


def candidate(state, view, *, now):
    return qualification(state,view,now=now).get('proof')


def recover(state, view, *, now):
    """Atomically append one opening. Old attempts and spend are never edited."""
    from .material_sec_config_recovery import recover as recover_local_config
    restored=recover_local_config(state,view,now=now)
    if restored is not None:return restored
    from . import material_requests as mr
    proof=candidate(state,view,now=now)
    if proof is None:return None
    deadline=(datetime.fromisoformat(now.replace('Z','+00:00'))+timedelta(hours=mr.DEFAULT_DEADLINE_HOURS)).isoformat(timespec='seconds')
    with mr._writer(state) as c:
        row=c.execute('SELECT * FROM material_requests WHERE request_id=?',(view['id'],)).fetchone()
        if row is None or row['content_hash']!=proof['request_hash']:return None
        current,closed,events=mr._status_of(c,view['id'])
        if (not closed or current not in ('exhausted','timeout') or not events
                or events[-1]['event_id']!=proof['closed_event_id']
                or any((e.get('source_queue_recovery')or{}).get('recovery_key')==proof['recovery_key'] for e in events)):
            return None
        body={'reopened':True,'reason':'SEC 已获授权但检索排队被误当作未接入；保留旧记录，正常恢复这一步',
              'deadline':deadline,'previous_status':current,'source_queue_recovery':proof}
        c.execute('INSERT INTO material_request_events(request_id,event_kind,status,requester_json,record_json,actor_ref,created_at) VALUES(?,?,?,?,?,?,?)',
                  (view['id'],'opened','open',None,mr._canonical(body),'automation:material-acquisition',now))
    return proof


def carried_attempts(events, latest_open_index, *, request_ref=None, request_hash=None):
    """Only the interrupted SEC rung starts again; other source history stands."""
    if latest_open_index < 0: return []
    marker=(events[latest_open_index].get('source_queue_recovery')or{})
    if not marker: return []
    from .material_sec_config_recovery import RULE_REF as CONFIG_RULE_REF
    if (marker.get('rule_ref')not in (RULE_REF,CONFIG_RULE_REF) or marker.get('source')!='sec_edgar'
            or marker.get('content_hash')!=content_hash({k:v for k,v in marker.items()if k!='content_hash'})):
        raise ValueError('source queue recovery marker is invalid')
    if (request_ref is not None and marker.get('request_ref')!=request_ref
            or request_hash is not None and marker.get('request_hash')!=request_hash
            or latest_open_index==0
            or events[latest_open_index-1].get('event_id')!=marker.get('closed_event_id'if marker.get('rule_ref')==RULE_REF else'parent_last_event_id')):
        raise ValueError('source queue recovery request/closed event binding changed')
    parent=next((i for i,e in enumerate(events[:latest_open_index])if e.get('event_id')==marker.get('parent_open_event_id')and e.get('event_kind')=='opened'),None)
    if parent is None:raise ValueError('source queue recovery parent is missing')
    return [e for e in events[parent+1:latest_open_index]
            if e.get('event_kind')=='attempt' and e.get('source')!='sec_edgar']
