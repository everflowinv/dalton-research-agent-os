"""One-call SEC archive continuation bound to a durable submissions response.

The versioned cursor uses the already published list_filings cursor field.
It never grants a second HTTP request inside the first Work.
"""
from __future__ import annotations
import json
import re
from collections.abc import Mapping
from datetime import date
from .store import canonical_json,content_hash
ROOT_CURSOR='sec-submissions-exact-pages:1'
RULE_REF='sec-submissions-archive-page:1'
class SecPageError(ValueError):pass

def archive_cursor(value):
    if not isinstance(value,str):raise SecPageError('SEC archive cursor must be text')
    try:body=json.loads(value)
    except ValueError as exc:raise SecPageError('SEC archive cursor is not JSON') from exc
    fields={'rule_ref','issuer','root_payload_hash','root_query_hash','name','filing_from','filing_to'}
    if not isinstance(body,dict) or set(body)!=fields or body['rule_ref']!=RULE_REF or canonical_json(body)!=value:
        raise SecPageError('SEC archive cursor closed identity differs')
    if (not isinstance(body['issuer'],str) or re.fullmatch(r'[0-9]{10}',body['issuer']) is None
            or re.fullmatch('CIK'+body['issuer']+r'-submissions-[0-9]{3}\.json',str(body['name'])) is None):
        raise SecPageError('SEC archive page is outside its issuer namespace')
    for key in ('root_payload_hash','root_query_hash'):
        if not isinstance(body[key],str) or re.fullmatch('[0-9a-f]{64}',body[key]) is None:raise SecPageError('SEC archive root hash is invalid')
    for key in ('filing_from','filing_to'):
        if not isinstance(body[key],str) or date.fromisoformat(body[key]).isoformat()!=body[key]:raise SecPageError('SEC archive coverage date is invalid')
    if body['filing_from']>body['filing_to']:raise SecPageError('SEC archive coverage is inverted')
    return body

def continuation(payload,parameters):
    files=payload.get('filings',{}).get('files')
    if not isinstance(files,list) or len(files)>64:raise SecPageError('SEC root lacks bounded archive descriptors')
    matches=[];names=set()
    for item in files:
        if not isinstance(item,Mapping):raise SecPageError('SEC archive descriptor is not an object')
        body={'rule_ref':RULE_REF,'issuer':parameters['issuer'],'root_payload_hash':content_hash(payload),
              'root_query_hash':content_hash({'operation':'list_filings','parameters':dict(parameters)}),
              'name':item.get('name'),'filing_from':item.get('filingFrom'),'filing_to':item.get('filingTo')}
        token=canonical_json(body);archive_cursor(token)
        if body['name'] in names:raise SecPageError('SEC archive descriptor is duplicated')
        names.add(body['name'])
        if body['filing_from']<=parameters['date_from']<=parameters['date_to']<=body['filing_to']:matches.append(token)
    if len(matches)!=1:raise SecPageError('SEC exact window needs one uniquely covering archive page')
    return matches[0]

def submissions_payload(payload,parameters):
    cursor=parameters.get('cursor')
    if cursor is None or cursor==ROOT_CURSOR:return payload
    bound=archive_cursor(cursor)
    if bound['issuer']!=parameters['issuer'] or not bound['filing_from']<=parameters['date_from']<=parameters['date_to']<=bound['filing_to']:
        raise SecPageError('SEC archive issuer or requested coverage differs')
    if not isinstance(payload,Mapping) or 'filings' in payload:raise SecPageError('SEC archive response must be a filing column document')
    # Archived SEC pages have no CIK header. Their issuer is bound by the
    # official root descriptor and exact endpoint, not inferred from accession.
    if 'cik' in payload and str(payload['cik']).zfill(10)!=parameters['issuer']:raise SecPageError('SEC archive CIK differs')
    return {'cik':parameters['issuer'],'filings':{'recent':dict(payload)}}

def _record(connection,table,key,ref):
    row=connection.execute('SELECT * FROM '+table+' WHERE '+key+'=?',(ref,)).fetchone()
    if row is None:raise SecPageError('SEC page parent authority is missing')
    wire=json.loads(row['record_json']);body=dict(wire);digest=body.pop('content_hash',None)
    if wire.get('id')!=ref or canonical_json(wire)!=row['record_json'] or digest!=row['content_hash'] or content_hash(body)!=digest:raise SecPageError('SEC page parent authority hash differs')
    for field in row.keys():
        if field in wire and not isinstance(wire[field],(dict,list)) and row[field]!=wire[field]:raise SecPageError('SEC page parent SQL projection differs')
    return wire

def envelope_parameters(connection,envelope):
    stored=_record(connection,'connector_source_envelopes','source_envelope_id',envelope['id'])
    if dict(envelope)!=stored:raise SecPageError('SEC supplied envelope differs from stored authority')
    inv=_record(connection,'connector_invocations','connector_invocation_id',envelope['connector_invocation_ref'])
    call=_record(connection,'connector_call_specs','call_spec_id',inv['call_spec_ref'])
    if (call['operation']!='list_filings'
            or inv['call_spec_hash']!=call['content_hash'] or inv['connector_profile_ref']!=envelope['connector_profile_ref']):
        raise SecPageError('SEC source invocation/call identity differs')
    return call['parameters']

def verify_archive_parent(connection,spool,parameters):
    """A caller token alone is insufficient: replay an existing official root."""
    bound=archive_cursor(parameters['cursor'])
    # One exact query hash, bounded candidate receipts. Every candidate must
    # retain full immutable authority and the original bytes to qualify.
    rows=connection.execute('SELECT e.source_envelope_id FROM connector_source_envelopes e '
        'JOIN connector_invocations i ON i.connector_invocation_id=e.connector_invocation_ref '
        'JOIN connector_call_specs c ON c.call_spec_id=i.call_spec_ref '
        'WHERE c.query_hash=? ORDER BY e.created_at DESC LIMIT 8',(bound['root_query_hash'],)).fetchall()
    for row in rows:
        e=_record(connection,'connector_source_envelopes','source_envelope_id',row['source_envelope_id'])
        if e['source']!='source:sec-edgar' or e['operation']!='list_filings' or e['status'] not in {'complete','partial','empty'}:continue
        p=envelope_parameters(connection,e)
        if (p.get('cursor')!=ROOT_CURSOR or {k:v for k,v in p.items() if k!='cursor'}!={k:v for k,v in parameters.items() if k!='cursor'}):continue
        raw=spool.read_object(e['raw_response_hash'])
        import hashlib
        if hashlib.sha256(raw).hexdigest()!=e['raw_response_hash']:raise SecPageError('SEC root original bytes differ')
        payload=json.loads(raw)
        if content_hash(payload)!=bound['root_payload_hash']:continue
        if e['cursor']!=parameters['cursor']:raise SecPageError('SEC root continuation differs')
        if continuation(payload,p)!=parameters['cursor']:raise SecPageError('SEC archive descriptor differs from original root')
        return e
    raise SecPageError('SEC archive cursor has no exact durable official root')
