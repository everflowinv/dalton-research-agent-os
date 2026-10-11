"""Exact MaterialRequest-bound SEC filing dates, never arbitrary overrides."""
from __future__ import annotations
import json
import sqlite3
from collections.abc import Mapping
from . import material_requests as mr
from . import material_acquisition as ma

class TargetedSecError(ValueError):
    pass

def _targeted_sec_parameters(state_dir, *, plan, company_ref, spec_ref, as_of,
                            mission_version_ref, mission_version_hash,
                            query_ref=None, binding=None, allow_closed=False):
    from .mission_source_discovery import build_discovery_parameters
    from .sec_filings_index_core import validate_filings_index_spec
    from datetime import date
    if plan["source_ref"] != "source:sec-edgar":
        raise TargetedSecError("targeted filing dates are only supported for SEC")
    if binding is not None:
        if not isinstance(binding, Mapping) or set(binding) != {
            "query_ref", "query_hash", "request_ref", "request_hash", "mission_version_ref",
            "mission_version_hash", "plan_ref", "plan_hash", "company_ref", "spec_ref"}:
            raise TargetedSecError("targeted binding has an invalid closed shape")
        query_ref = binding["query_ref"]
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
    return validate_filings_index_spec(parameters),actual



def targeted_sec_parameters(state_dir, **kwargs):
    """Fail closed on an unreadable source; no malformed row crashes a lane tick."""
    try:
        return _targeted_sec_parameters(state_dir, **kwargs)
    except TargetedSecError:
        raise
    except (ValueError, TypeError, KeyError, sqlite3.Error, OSError) as exc:
        raise TargetedSecError(f"targeted material authority is unreadable: {type(exc).__name__}") from exc
