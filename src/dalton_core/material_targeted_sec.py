"""Exact MaterialRequest-bound SEC filing dates, never arbitrary overrides."""
from __future__ import annotations
import json
import sqlite3
from collections.abc import Mapping
from pathlib import Path
from . import material_requests as mr
from . import material_acquisition as ma
from .raw_spool import RawSpoolError

class TargetedSecError(ValueError):
    pass

def _targeted_sec_parameters(state_dir, *, plan, company_ref, spec_ref, as_of,
                            mission_version_ref, mission_version_hash,
                            query_ref=None, binding=None, allow_closed=False, cursor=None, spool_dir=None, archive_parent=None):
    from .mission_source_discovery import build_discovery_parameters
    from .sec_filings_index_core import validate_filings_index_spec
    from datetime import date
    if plan["source_ref"] != "source:sec-edgar":
        raise TargetedSecError("targeted filing dates are only supported for SEC")
    from .sec_submissions_pages import RULE_REF
    binding_fields={"query_ref", "query_hash", "request_ref", "request_hash", "mission_version_ref",
            "mission_version_hash", "plan_ref", "plan_hash", "company_ref", "spec_ref"}
    paged=binding is None or (isinstance(binding,Mapping) and binding.get("page_rule_ref")==RULE_REF)
    if binding is not None:
        if (not isinstance(binding,Mapping) or set(binding) not in (binding_fields,binding_fields|{"page_rule_ref"},binding_fields|{"page_rule_ref","archive_parent"})
                or ("page_rule_ref" in binding and binding["page_rule_ref"]!=RULE_REF)):
            raise TargetedSecError("targeted binding has an invalid closed shape")
        query_ref = binding["query_ref"]
        archive_parent = binding.get("archive_parent")
    if not isinstance(query_ref, str) or not query_ref.startswith("material-targeted-query:"):
        raise TargetedSecError("an exact targeted query reference is required")
    path=mr.ledger_path(state_dir)
    if not path.is_file():
        raise TargetedSecError("targeted material ledger is missing")
    from .readonly_sqlite import connect_read_only
    connection=connect_read_only(path)
    connection.row_factory=sqlite3.Row
    try:
        connection.execute("PRAGMA busy_timeout=1000");connection.execute("BEGIN")
        row=connection.execute("SELECT * FROM material_targeted_queries WHERE query_id=?",(query_ref,)).fetchone()
        if row is None: raise TargetedSecError("targeted query was not found")
        query=json.loads(row["record_json"])
        if (not isinstance(query,dict) or set(query) != {"schema_version","request_id","source","source_ref",
                "company_ref","spec_ref","phrasings","period","created_at"}
                or query["schema_version"] != ma.SCHEMA_VERSION
                or any(query[k]!=row[k] for k in ("request_id","source","source_ref","company_ref","spec_ref","created_at"))
                or ma._canonical(query)!=row["record_json"]
                or query_ref != "material-targeted-query:"+ma._sha({k:query[k] for k in (
                    "request_id","source","company_ref","spec_ref","created_at")})[:32]):
            raise TargetedSecError("targeted query identity drifted")
        reqrow=connection.execute("SELECT * FROM material_requests WHERE request_id=?",(query["request_id"],)).fetchone()
        if reqrow is None: raise TargetedSecError("material request was not found")
        request=json.loads(reqrow["record_json"])
        if (request.get("content_hash")!=reqrow["content_hash"]
                or request.get("content_hash")!=mr._sha({k:v for k,v in request.items() if k!="content_hash"})
                or mr._canonical(request)!=reqrow["record_json"]
                or any(request.get(k)!=reqrow[col] for k,col in (("id","request_id"),("digest","digest"),
                    ("subject","subject_ref"),("period","period"),("deadline","deadline"),
                    ("created_at","created_at"),("actor_ref","actor_ref")))
                or request.get("schema_version")!=mr.SCHEMA_VERSION
                or request.get("need",{}).get("kind")!=reqrow["need_kind"]
                or request.get("requester",{}).get("lane")!=reqrow["requester_lane"]
                or request.get("id")!="material-request:"+request["digest"][:32]
                or request["digest"]!=mr.request_digest(request["subject"],request["need"],request["period"])):
            raise TargetedSecError("material request identity/hash drifted")
        status,closed,_=mr._status_of(connection,request["id"])
        if not allow_closed and (closed or status not in mr.ACTIVE):
            raise TargetedSecError("material request is no longer active")
    finally:
        connection.close()
    if (query["source"]!="sec_edgar" or query["source_ref"]!="source:sec-edgar"
            or query["company_ref"]!=company_ref or request["subject"]!=company_ref
            or query["period"]!=request["period"] or "sec_edgar" not in request["source_priority"]
            or company_ref not in plan["companies"]):
        raise TargetedSecError("targeted request company/source scope drifted")
    if (query["spec_ref"] != spec_ref or spec_ref not in [s["spec_ref"] for s in plan["specs"]]
            or ma.TargetedSourceRung("sec_edgar").spec_for(request) != spec_ref):
        raise TargetedSecError("targeted query spec is not authorized by this plan")
    phrases=query["phrasings"]
    if (not isinstance(phrases,list) or not 1<=len(phrases)<=3
            or any(not isinstance(s,str) or not s.strip() or len(s)>390 for s in phrases)
            or phrases != ma.build_queries(request,ma.identity_for(state_dir,company_ref),now=query["created_at"])):
        raise TargetedSecError("targeted phrasings do not match the actual material request")
    actual={"query_ref":query_ref,"query_hash":ma._sha(query),"request_ref":request["id"],
        "request_hash":request["content_hash"],"mission_version_ref":mission_version_ref,
        "mission_version_hash":mission_version_hash,"plan_ref":plan["id"],"plan_hash":plan["content_hash"],
        "company_ref":company_ref,"spec_ref":spec_ref}
    if paged:actual["page_rule_ref"]=RULE_REF
    if archive_parent is not None:
        if not paged or cursor is None:raise TargetedSecError("archive parent requires a paged continuation")
        actual["archive_parent"]=archive_parent
    if binding is not None and dict(binding)!=actual:
        raise TargetedSecError("exact targeted request/plan/mission binding drifted")
    parameters=build_discovery_parameters(plan,spec_ref=spec_ref,company_ref=company_ref,as_of=as_of)
    acceptance=request["acceptance"]
    start,end=acceptance.get("date_from"),acceptance.get("date_to")
    if start is not None or end is not None:
        if (not isinstance(start,str) or not isinstance(end,str)
                or date.fromisoformat(start).isoformat()!=start or date.fromisoformat(end).isoformat()!=end
                or not parameters["date_from"] <= start <= end <= parameters["date_to"]):
            raise TargetedSecError("targeted filing dates exceed the authorized plan window")
        parameters.update(date_from=start,date_to=end)
    accession=acceptance.get("accession")
    if accession is not None:
        import re
        if (not isinstance(accession,str) or re.fullmatch(r"[0-9]{10}-[0-9]{2}-[0-9]{6}",accession) is None
                or acceptance.get("cik") != parameters["issuer"]
                or acceptance.get("document_ref") != "sec:filing:"+accession or start != end or start is None):
            raise TargetedSecError("targeted exact filing identity differs")
    if accession is not None and paged:
        from .sec_submissions_pages import ROOT_CURSOR
        parameters["cursor"] = ROOT_CURSOR if cursor is None else cursor
    elif cursor is not None:
        raise TargetedSecError("archive continuation requires an exact filing request")
    parameters=validate_filings_index_spec(parameters)
    if cursor is not None:
        from .sec_submissions_pages import verify_archive_parent
        from .raw_spool import RawSpoolReader
        c=connect_read_only(Path(state_dir)/"core.sqlite");c.row_factory=sqlite3.Row
        try:
            if archive_parent is not None:
                from .sec_submissions_pages import _record
                parent=_record(c,'coverage_mission_source_discoveries','record_id',archive_parent['discovery_ref'])
                if parent['company_ref']!=company_ref or parent['mission_version_ref']!=mission_version_ref:
                    raise TargetedSecError('SEC archive parent company/mission differs')
            verify_archive_parent(c,RawSpoolReader(spool_dir or Path(state_dir)/"connector-spool"),parameters,parent_binding=archive_parent)
        finally:c.close()
    return parameters,actual



def targeted_sec_parameters(state_dir, **kwargs):
    """Fail closed on an unreadable source; no malformed row crashes a lane tick."""
    try:
        return _targeted_sec_parameters(state_dir, **kwargs)
    except TargetedSecError:
        raise
    except (ValueError, TypeError, KeyError, sqlite3.Error, OSError, RawSpoolError) as exc:
        raise TargetedSecError(f"targeted material authority is unreadable: {type(exc).__name__}") from exc


def pending_archive_query(state_dir,connection,plan,launcher,*,query_ref=None,deadline=None):
    """One continuation of an exact targeted query, after its root is durable.

    The latest targeted event must still name the successful root ticket.
    Once an archive is launched (even if it fails), ordinary material retry
    owns recovery; an older root cannot reopen the same page on every tick.
    """
    from .readonly_sqlite import connect_read_only
    path=mr.ledger_path(state_dir)
    if not path.is_file():return None
    c=connect_read_only(path);c.row_factory=sqlite3.Row
    try:
        from .source_discovery_read_budget import bounded_rows
        rows=bounded_rows(c,"SELECT q.query_id,q.record_json,json_extract(e.record_json,'$.ticket_ref') AS ticket_ref,json_extract(e.record_json,'$.dispatch_ref') AS dispatch_ref FROM material_targeted_queries q "
            "JOIN material_targeted_query_events e ON e.event_id=(SELECT MAX(x.event_id) FROM material_targeted_query_events x WHERE x.query_id=q.query_id AND x.status IN ('launched','failed','refused')) "
            "WHERE q.source_ref='source:sec-edgar' AND e.status='launched' AND json_extract(e.record_json,'$.kind')='archive_ready' "
            +('AND q.query_id=? ' if query_ref is not None else '')
            +'ORDER BY e.event_id DESC LIMIT 32', (() if query_ref is None else (query_ref,)),deadline=deadline)
    finally:c.close()
    from datetime import date
    from .sec_submissions_pages import ROOT_CURSOR,_record
    for row in rows:
        try:
            ticket=launcher.status(row['ticket_ref']);summary=ticket.get('summary') or {}
            if ticket['status']!='succeeded' or ticket.get('cursor') is not None or not summary.get('next_cursor') or ticket.get('targeted_binding') is None:continue
            dispatch=connection.execute('SELECT status,ticket_ref FROM coverage_mission_discovery_dispatches WHERE dispatch_id=?',(row['dispatch_ref'],)).fetchone()
            if dispatch is None or dispatch['status']!='succeeded' or dispatch['ticket_ref']!=row['ticket_ref']:continue
            query=json.loads(row['record_json']);b=ticket['targeted_binding']
            p,bound=targeted_sec_parameters(state_dir,plan=plan,company_ref=query['company_ref'],spec_ref=query['spec_ref'],
                as_of=date.fromisoformat(ticket['as_of']),mission_version_ref=ticket['mission_version_ref'],
                mission_version_hash=ticket['mission_version_hash'],binding=b)
            if summary.get('parameters')!=p or summary.get('targeted_binding')!=bound or p.get('cursor')!=ROOT_CURSOR:continue
            discovery=_record(connection,'coverage_mission_source_discoveries','record_id',summary['discovery_ref'])
            if (discovery['content_hash']!=summary['discovery_hash'] or discovery['parameters']!=p
                    or discovery['query_hash']!=summary['query_hash'] or discovery['company_ref']!=query['company_ref']):continue
            envelope=_record(connection,'connector_source_envelopes','source_envelope_id',discovery['source_envelope_ref'])
            if envelope['content_hash']!=discovery['source_envelope_hash'] or envelope['cursor']!=summary['next_cursor']:continue
            return {'query_id':row['query_id'],**query,'archive_cursor':envelope['cursor'],
                'archive_parent':{'discovery_ref':discovery['id'],'discovery_hash':discovery['content_hash'],
                    'envelope_ref':envelope['id'],'envelope_hash':envelope['content_hash']}}
        except (ValueError,KeyError,TypeError,OSError):
            continue  # No unreadable or closed parent can authorize a page.
    return None


def archive_pending_for_material(state_dir,query_ref):
    from .readonly_sqlite import connect_read_only
    from .sec_submissions_pages import _record,ROOT_CURSOR
    from .store import content_hash,canonical_json
    c=connect_read_only(mr.ledger_path(state_dir));c.row_factory=sqlite3.Row
    try:
        row=c.execute("SELECT json_extract(record_json,'$.ticket_ref') AS ticket_ref FROM material_targeted_query_events WHERE query_id=? AND status IN ('launched','failed','refused') ORDER BY event_id DESC LIMIT 1",(query_ref,)).fetchone()
    finally:c.close()
    if row is None or not str(row['ticket_ref']).startswith('sec-filings-index:'):return False
    directory=Path(state_dir)/'sec-filings-index-runs'/row['ticket_ref'].split(':',1)[1]
    try:ticket=json.loads((directory/'ticket.json').read_text());summary=json.loads((directory/'summary.json').read_text())
    except (OSError,ValueError):return False
    if ticket.get('cursor') is not None or summary.get('status')!='succeeded' or not summary.get('next_cursor'):return False
    c=connect_read_only(Path(state_dir)/'core.sqlite');c.row_factory=sqlite3.Row
    try:
        d=_record(c,'coverage_mission_source_discoveries','record_id',summary['discovery_ref'])
        e=_record(c,'connector_source_envelopes','source_envelope_id',d['source_envelope_ref'])
        return (d['content_hash']==summary['discovery_hash'] and d['parameters']==summary['parameters']
                and d['parameters'].get('cursor')==ROOT_CURSOR and e['cursor']==summary['next_cursor']
                and e['content_hash']==d['source_envelope_hash']
                and ticket.get('targeted_binding',{}).get('query_ref')==query_ref)
    finally:c.close()
