"""Exact native annual identities; 6-K disclosures are never quarterly filings.

The approved statements parser still owns parsing. Native v6 additionally
retains its complete attachment index and the exact bytes of the statement
source members, beside the untouched parsed artifact. This is not an IFRS to
US-GAAP mapping and does not assert that the issuer reports quarterly.
"""
from __future__ import annotations

import base64
import hashlib
import json
import re
from datetime import date, datetime
from urllib.parse import urlsplit

from .store import content_hash

CONTRACT = 'sec-native-annual-document-set:0.1'
ANNUAL_FORMS = frozenset(('20-F', '20-F/A', '40-F', '40-F/A'))
MAX_MEMBERS = 256
MAX_MEMBER_BYTES = 32 * 1024 * 1024
MAX_TOTAL_BYTES = 48 * 1024 * 1024
_NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,255}')
_ACCESSION = re.compile(r'\d{10}-\d{2}-\d{6}')
_DESCRIPTOR = frozenset(('accession', 'form', 'filed', 'report_date', 'primary_document', 'is_xbrl'))
_MEMBER = frozenset(('document', 'document_type', 'sequence', 'description', 'url'))


class NativeAnnualError(ValueError):
    pass


def verify_native_approval(governance, *, at=None):
    from datetime import timezone
    from .sec_financials_core import sec_financials_identity
    expected = sec_financials_identity(version=6)
    if (not governance.approved or governance.id != "connector-governance:sec-financial-statements:v6"
            or governance.wire["expected_source_hash"] != expected["source_hash"]
            or governance.wire["expected_schema_hash"] != expected["schema_hash"]):
        raise NativeAnnualError("native annual approval is not the exact v6 contract")
    clock = datetime.now(timezone.utc) if at is None else governance._parse_time(at, "native_approved_at")
    if (clock < governance._parse_time(governance.effective_from, "effective_from")
            or (governance.wire["effective_until"] is not None
                and clock >= governance._parse_time(governance.wire["effective_until"], "effective_until"))):
        raise NativeAnnualError("native annual approval is outside its effective window")
    return governance


def descriptor(value):
    if not isinstance(value, dict) or set(value) != _DESCRIPTOR:
        raise NativeAnnualError('native annual descriptor shape differs')
    if (_ACCESSION.fullmatch(str(value['accession'])) is None
            or value['form'] not in ANNUAL_FORMS or value['is_xbrl'] is not True
            or _NAME.fullmatch(str(value['primary_document'])) is None):
        raise NativeAnnualError('native annual requires the exact original annual XBRL descriptor')
    for key in ('filed', 'report_date'):
        if date.fromisoformat(value[key]).isoformat() != value[key]:
            raise NativeAnnualError('native annual date is not canonical')
    return dict(value)


def annual_candidates(connection, spool, profile, *, company_ref, issuer, as_of, limit):
    """Select only original annual XBRL rows from the strongly bound root."""
    from .sec_issuer_reporting_profile import verify_profile, MAX_ROOT_BYTES, MAX_RECENT_FILINGS
    if type(limit) is not int or not 1 <= limit <= 8:
        raise NativeAnnualError('native annual selection limit must be 1..8')
    profile = verify_profile(connection, spool, profile, company_ref=company_ref, issuer=issuer, as_of=as_of)
    if profile['family'] not in ('foreign_20f', 'foreign_40f'):
        raise NativeAnnualError('native annual reporting family is not foreign')
    reader = spool() if callable(spool) else spool
    raw = reader.read_object(profile['raw_hash'])
    if len(raw) > MAX_ROOT_BYTES or hashlib.sha256(raw).hexdigest() != profile['raw_hash']:
        raise NativeAnnualError('native annual profile original differs')
    root = json.loads(raw)
    recent = root['filings']['recent']
    fields = ('accessionNumber', 'form', 'filingDate', 'reportDate', 'primaryDocument', 'isXBRL')
    size = len(recent['form'])
    if size > MAX_RECENT_FILINGS or any(not isinstance(recent.get(k), list) or len(recent[k]) != size for k in fields):
        raise NativeAnnualError('native annual index lacks complete reporting/XBRL columns')
    family_form = '40-F' if profile['family'] == 'foreign_40f' else '20-F'
    selected = []
    for i, form in enumerate(recent['form']):
        if form != family_form or recent['isXBRL'][i] != 1:
            continue
        filed = date.fromisoformat(recent['filingDate'][i])
        if filed > as_of:
            continue
        selected.append(descriptor({'accession': recent['accessionNumber'][i], 'form': form,
            'filed': filed.isoformat(), 'report_date': recent['reportDate'][i],
            'primary_document': recent['primaryDocument'][i], 'is_xbrl': True}))
    # Automatic first-package acquisition selects the original annual. An
    # amendment is not an independent filed year and needs a separate exact
    # replacement/role proof before it can replace this source selection.
    selected.sort(key=lambda x: (x['report_date'], x['filed'], x['accession']), reverse=True)
    if len({x['accession'] for x in selected}) != len(selected):
        raise NativeAnnualError('native annual original index repeats an accession')
    return selected[:limit]


def member(value, *, issuer, accession):
    if not isinstance(value, dict) or set(value) != _MEMBER:
        raise NativeAnnualError('native annual attachment identity shape differs')
    if (re.fullmatch(r'\d{10}', issuer) is None or _ACCESSION.fullmatch(accession) is None
            or any(not isinstance(value[k], str) for k in _MEMBER)
            or _NAME.fullmatch(value['document']) is None):
        raise NativeAnnualError('native annual attachment identity is invalid')
    url = urlsplit(value['url'])
    path = f'/Archives/edgar/data/{int(issuer)}/{accession.replace("-", "")}/{value["document"]}'
    if (url.scheme != 'https' or url.hostname not in ('www.sec.gov', 'sec.gov')
            or url.port not in (None, 443) or url.username or url.password
            or url.query or url.fragment or url.path != path):
        raise NativeAnnualError('native annual attachment issuer/accession/filename URL differs')
    return dict(value)


def source_member(value, primary):
    # Preserve all possible financial text and XBRL members, including annual
    # exhibits. Graphics do not enter parsing; their index identities remain.
    return (value['document'] == primary or value['document'].lower().endswith(('.xml', '.xsd', '.htm', '.html', '.txt', '.pdf')))


def build_document_set(*, issuer, filing, reporting_profile_hash, members, originals):
    filing = descriptor(filing)
    if (not isinstance(reporting_profile_hash, str) or re.fullmatch(r'[0-9a-f]{64}', reporting_profile_hash) is None
            or not isinstance(members, list) or not 1 <= len(members) <= MAX_MEMBERS):
        raise NativeAnnualError('native annual document set profile or index bound differs')
    checked = [member(x, issuer=issuer, accession=filing['accession']) for x in members]
    if len({x['document'] for x in checked}) != len(checked):
        raise NativeAnnualError('native annual attachment index repeats a document')
    checked.sort(key=lambda x: (x['sequence'], x['document']))
    if sum(x['document'] == filing['primary_document'] for x in checked) != 1:
        raise NativeAnnualError('native annual primary is missing from its original index')
    wanted = {x['document'] for x in checked if source_member(x, filing['primary_document'])}
    if not isinstance(originals, dict) or set(originals) != wanted:
        raise NativeAnnualError('native annual statement source member is missing or foreign')
    bodies = []
    total = 0
    for name in sorted(wanted):
        raw = originals[name]
        if not isinstance(raw, bytes) or not raw or len(raw) > MAX_MEMBER_BYTES:
            raise NativeAnnualError('native annual original member bytes unavailable or exceed bound')
        total += len(raw)
        if total > MAX_TOTAL_BYTES:
            raise NativeAnnualError('native annual original document set exceeds total bound')
        bodies.append({'document': name, 'sha256': hashlib.sha256(raw).hexdigest(), 'bytes': len(raw),
                       'base64': base64.b64encode(raw).decode('ascii')})
    body = {'contract_ref': CONTRACT, 'issuer': issuer, 'filing': filing,
            'reporting_profile_hash': reporting_profile_hash, 'members': checked, 'originals': bodies,
            'transport_provenance': 'approved-statements-parser-public-https'}
    return {**body, 'content_hash': content_hash(body)}


def verify_document_set(value, *, issuer, filing, reporting_profile_hash):
    if not isinstance(value, dict) or set(value) != {
            'contract_ref', 'issuer', 'filing', 'reporting_profile_hash', 'members', 'originals',
            'transport_provenance', 'content_hash'} or value['contract_ref'] != CONTRACT:
        raise NativeAnnualError('native annual document set shape differs')
    originals = {}
    if not isinstance(value['originals'], list):
        raise NativeAnnualError('native annual source originals shape differs')
    for item in value['originals']:
        if not isinstance(item, dict) or set(item) != {'document', 'sha256', 'bytes', 'base64'} or item['document'] in originals:
            raise NativeAnnualError('native annual source original identity differs')
        raw = base64.b64decode(item['base64'], validate=True)
        if hashlib.sha256(raw).hexdigest() != item['sha256'] or len(raw) != item['bytes']:
            raise NativeAnnualError('native annual source original bytes differ')
        originals[item['document']] = raw
    expected = build_document_set(issuer=issuer, filing=filing, reporting_profile_hash=reporting_profile_hash,
                                 members=value['members'], originals=originals)
    if value != expected:
        raise NativeAnnualError('native annual document set binding differs')
    return expected


def validate_annual_lines(lines, report_date):
    """A native annual must expose all three roles at its actual annual end.

    Context durations are retained as reported. Comparative/H1/9M facts can
    coexist; they are never counted as independent quarter completions.
    """
    seen = set()
    for line in lines:
        if line.get('period_end') != report_date or line.get('dimensions') != {}:
            continue
        start = line.get('period_start')
        if line.get('statement') == 'balance' and start is None:
            seen.add('balance')
        elif line.get('statement') in ('income', 'cash') and isinstance(start, str):
            days = (date.fromisoformat(report_date) - date.fromisoformat(start)).days + 1
            if 290 < days <= 380:
                seen.add(line['statement'])
    if seen != {'income', 'balance', 'cash'}:
        raise NativeAnnualError('native annual lacks complete income/balance/cash roles at the reported annual end')

REQUEST_CONTRACT = 'sec-native-annual-request:0.1'


def make_request(*, authorization, reporting_profile, filings, as_of):
    """Immutable acquisition scope, independent of later source/profile changes."""
    if not isinstance(as_of, date) or isinstance(as_of, datetime):
        raise NativeAnnualError('native annual request requires a calendar date')
    checked = [descriptor(x) for x in filings]
    if not 1 <= len(checked) <= 8 or len({x['accession'] for x in checked}) != len(checked):
        raise NativeAnnualError('native annual request filing set differs')
    family = reporting_profile.get('family')
    form = {'foreign_20f':'20-F', 'foreign_40f':'40-F'}.get(family)
    if form is None or any(x['form'].removesuffix('/A') != form for x in checked):
        raise NativeAnnualError('native annual request forms differ from reporting family')
    if (authorization.get('company_ref') != reporting_profile.get('company_ref')
            or any(date.fromisoformat(x['filed']) > as_of for x in checked)):
        raise NativeAnnualError('native annual request company or filing date differs')
    body = {'contract_ref': REQUEST_CONTRACT, 'authorization': dict(authorization),
            'reporting_profile': dict(reporting_profile), 'filings': checked, 'as_of': as_of.isoformat()}
    return {**body, 'content_hash':content_hash(body)}


def verify_request(connection, spool, request, *, current_authorization=None):
    if not isinstance(request, dict) or set(request) != {
            'contract_ref', 'authorization', 'reporting_profile', 'filings', 'as_of', 'content_hash'}:
        raise NativeAnnualError('native annual request closed shape differs')
    expected = make_request(authorization=request['authorization'], reporting_profile=request['reporting_profile'],
                            filings=request['filings'], as_of=date.fromisoformat(request['as_of']))
    if request != expected:
        raise NativeAnnualError('native annual request immutable identity differs')
    if current_authorization is not None and request['authorization'] != current_authorization:
        raise NativeAnnualError('native annual request current mission authorization differs')
    profile = request['reporting_profile']
    candidates = annual_candidates(connection, spool, profile, company_ref=request['authorization']['company_ref'],
        issuer=profile['issuer'], as_of=date.fromisoformat(request['as_of']), limit=len(request['filings']))
    if request['filings'] != candidates:
        raise NativeAnnualError('native annual request is not the original bounded annual selection')
    return request


def compact_document_set(value):
    body = {k:v for k,v in value.items() if k not in ('originals', 'content_hash')}
    body['originals'] = [{k:v for k,v in x.items() if k != 'base64'} for x in value['originals']]
    body['raw_document_set_hash'] = value['content_hash']
    return {**body, 'content_hash': content_hash(body)}


def document_set_schema():
    """Closed metadata wire; source bytes stay in the canonical raw artifact."""
    text = {'type':'string','minLength':1}
    digest = {'type':'string','pattern':'^[0-9a-f]{64}$'}
    desc = {'type':'object','additionalProperties':False,'properties':{
        'accession':text,'form':{'type':'string','enum':sorted(ANNUAL_FORMS)},
        'filed':text,'report_date':text,'primary_document':text,'is_xbrl':{'type':'boolean','enum':[True]}},
        'required':sorted(_DESCRIPTOR)}
    attachment = {'type':'object','additionalProperties':False,
                  'properties':{k:(text if k in ('document','url') else {'type':'string'}) for k in _MEMBER},'required':sorted(_MEMBER)}
    original = {'type':'object','additionalProperties':False,'properties':{
        'document':text,'sha256':digest,'bytes':{'type':'integer','minimum':1,'maximum':MAX_MEMBER_BYTES}},
        'required':['document','sha256','bytes']}
    fields = {'contract_ref':{'type':'string','enum':[CONTRACT]},'issuer':{'type':'string','pattern':'^[0-9]{10}$'},
              'filing':desc,'reporting_profile_hash':digest,
              'members':{'type':'array','minItems':1,'maxItems':MAX_MEMBERS,'items':attachment},
              'originals':{'type':'array','minItems':1,'maxItems':MAX_MEMBERS,'items':original},
              'transport_provenance':{'type':'string','enum':['approved-statements-parser-public-https']},
              'raw_document_set_hash':digest,'content_hash':digest}
    return {'type':['object','null'],'additionalProperties':False,'properties':fields,'required':sorted(fields)}


def load_admitted_request(state_dir, request_path, *, spool_dir=None, require_current=False):
    from contextlib import closing
    from pathlib import Path
    import sqlite3
    from .readonly_sqlite import connect_read_only
    from .raw_spool import RawSpoolReader
    from .coverage_mission import CoverageMissionAuthority
    state = Path(state_dir).resolve()
    path = Path(request_path)
    if (path.is_symlink() or path.parent.resolve() != state / 'sec-native-statement-requests'
            or re.fullmatch(r'[0-9a-f]{32}\.json', path.name) is None or path.stat().st_size > 1_000_000):
        raise NativeAnnualError('native annual admitted request path differs')
    packet = json.loads(path.read_bytes())
    if not isinstance(packet, dict) or set(packet) != {'dispatch_ref','request','content_hash'}:
        raise NativeAnnualError('native annual admitted packet shape differs')
    if content_hash({k:v for k,v in packet.items() if k!='content_hash'}) != packet['content_hash']:
        raise NativeAnnualError('native annual admitted packet hash differs')
    dispatch_ref = 'mission-statement-dispatch:'+path.stem
    if packet['dispatch_ref'] != dispatch_ref:
        raise NativeAnnualError('native annual packet dispatch identity differs')
    with closing(connect_read_only(state / 'core.sqlite')) as connection:
        connection.row_factory=sqlite3.Row
        missions=object.__new__(CoverageMissionAuthority);missions.connection=connection
        held=missions.native_statement_request(dispatch_ref)
        dispatch=missions._statement_dispatch(dispatch_ref)
        if held != packet['request'] or dispatch['status'] not in ('pending','launched','succeeded'):
            raise NativeAnnualError('native annual request differs from immutable owner admission')
        current=None
        if require_current:
            current=missions.sec_lane_authorization_for_company(dispatch['company_ref'])
        return verify_request(connection,RawSpoolReader(spool_dir or state/'connector-spool'),held,
                              current_authorization=current)


def profile_for_company(missions, spool, company_ref, *, as_of):
    """Bounded exact root lookup; identity comes from its original request."""
    from .sec_submissions_pages import _record, ROOT_CURSOR
    from .sec_issuer_reporting_profile import current_profile
    auth=missions.sec_lane_authorization_for_company(company_ref)
    rows=missions.connection.execute(
        "SELECT record_id FROM coverage_mission_source_discoveries "
        "WHERE mission_version_ref IN (SELECT mission_version_id FROM coverage_mission_versions WHERE mission_ref=?) "
        "AND company_ref=? AND source_ref='source:sec-edgar' ORDER BY created_at DESC,record_id DESC LIMIT 8",
        (auth['mission_ref'],company_ref)).fetchall()
    for row in rows:
        discovery=_record(missions.connection,'coverage_mission_source_discoveries','record_id',row[0])
        if discovery.get('parameters',{}).get('cursor') not in (None,ROOT_CURSOR):
            continue
        issuer=discovery.get('parameters',{}).get('issuer')
        if not isinstance(issuer,str) or re.fullmatch(r'\d{10}',issuer) is None:
            raise NativeAnnualError('native annual reporting root issuer is invalid')
        return current_profile(missions.connection,spool,mission_version_ref=auth['mission_version_ref'],
                               company_ref=company_ref,issuer=issuer,as_of=as_of)
    return None


def prepare_evidence(connection, state_dir, *, dispatch, request, filing, cik, source_refs, governance_ref, governance_hash):
    """Rebuild new authority from the exact kept parse and source document set."""
    from pathlib import Path
    from .acquired_sec_filings import _spool_bytes
    from .sec_financials_normalise import normalise_filing
    if str(cik).zfill(10) != request['reporting_profile']['issuer']:
        raise NativeAnnualError('native annual observation issuer differs from original request')
    expected = next((x for x in request['filings'] if x['accession']==filing['accession']),None)
    if expected is None or any(filing[k]!=expected[k] for k in ('form','filed','report_date')):
        raise NativeAnnualError('native annual observation is outside exact admitted filing set')
    refs = [x for x in source_refs if isinstance(x,str) and x.startswith('raw-sink:')]
    if len(refs)!=1:
        raise NativeAnnualError('native annual requires one exact complete parsed artifact')
    raw, problem = _spool_bytes(Path(state_dir),refs[0][9:])
    if raw is None:
        raise NativeAnnualError('native annual parsed source artifact unavailable: '+str(problem))
    payload=json.loads(raw)
    from .connector_governance import ConnectorGovernance, ConnectorGovernanceError
    if not isinstance(payload.get('native_approved_at'), str):
        raise NativeAnnualError('native annual original approval timestamp is missing')
    try:
        approved = verify_native_approval(ConnectorGovernance(payload.get('native_governance') or {}),
                                         at=payload['native_approved_at'])
    except ConnectorGovernanceError as exc:
        raise NativeAnnualError('native annual original approval proof differs') from exc
    if approved.id != governance_ref or approved.content_hash != governance_hash:
        raise NativeAnnualError('native annual kept approval differs from observation')
    if str(payload.get('cik','')).zfill(10)!=request['reporting_profile']['issuer']:
        raise NativeAnnualError('native annual parsed source artifact issuer differs')
    from .sec_issuer_reporting_profile import MAX_ROOT_BYTES
    encoded = payload.get('native_profile_original')
    if not isinstance(encoded, str) or len(encoded) > 4 * ((MAX_ROOT_BYTES + 2) // 3):
        raise NativeAnnualError('native annual original submissions root is unavailable')
    try:
        root_bytes = base64.b64decode(encoded, validate=True)
    except ValueError as exc:
        raise NativeAnnualError('native annual original submissions encoding differs') from exc
    class OriginalRootReader:
        def read_object(self, digest):
            if digest != request['reporting_profile']['raw_hash'] or hashlib.sha256(root_bytes).hexdigest() != digest:
                raise NativeAnnualError('native annual original submissions hash differs')
            return root_bytes
    verify_request(connection, OriginalRootReader(), request)
    original=[x for x in payload.get('filings',[]) if x.get('accession')==filing['accession']]
    if len(original)!=1:
        raise NativeAnnualError('native annual parsed source artifact is not unique')
    parsed=original[0]
    if any(parsed.get(k)!=expected[k] for k in ('form','filed','report_date')):
        raise NativeAnnualError('native annual parsed source artifact header differs')
    docset=verify_document_set(parsed.get('native_document_set'),issuer=request['reporting_profile']['issuer'],
                              filing=expected,reporting_profile_hash=request['reporting_profile']['content_hash'])
    compact=compact_document_set(docset)
    if filing.get('native_document_set')!=compact:
        raise NativeAnnualError('native annual document set metadata differs from kept originals')
    result=normalise_filing(accession=parsed['accession'],form=parsed['form'],filed=parsed['filed'],
                            report_date=parsed['report_date'],statements=parsed.get('statements') or {})
    if result['lines']!=filing['lines']:
        raise NativeAnnualError('native annual statement values/roles/context differ from kept parser artifact')
    validate_annual_lines(result['lines'],expected['report_date'])
    body={'contract_ref':CONTRACT,'company_ref':dispatch['company_ref'],'accession':expected['accession'],
          'native_request_hash':request['content_hash'],'document_set':compact,'raw_parse_ref':refs[0],
          'governance_ref':approved.id,'governance_hash':approved.content_hash}
    return {**body,'content_hash':content_hash(body)}


def stored_evidence(connection, filing):
    """Native readers require the independently kept immutable source evidence."""
    row=connection.execute('SELECT record_json,content_hash FROM coverage_mission_statement_native_evidence WHERE ingest_id=?',
                           (filing['ingest_id'],)).fetchone()
    if row is None:
        raise NativeAnnualError('native annual immutable evidence is missing')
    value=json.loads(row['record_json'])
    if value.get('content_hash')!=row['content_hash'] or content_hash({k:v for k,v in value.items() if k!='content_hash'})!=row['content_hash']:
        raise NativeAnnualError('native annual immutable evidence hash differs')
    from .coverage_mission import CoverageMissionAuthority
    authority=object.__new__(CoverageMissionAuthority);authority.connection=connection
    dispatch=authority._statement_dispatch(filing['dispatch_id'])
    request=authority.native_statement_request(filing['dispatch_id'])
    if request is None or request['content_hash']!=value['native_request_hash']:
        raise NativeAnnualError('native annual kept request binding differs')
    path=connection.execute('PRAGMA database_list').fetchone()[2]
    if not path:
        raise NativeAnnualError('native annual reader requires the kept source artifact location')
    rows=connection.execute('SELECT * FROM coverage_mission_statement_lines WHERE ingest_id=? ORDER BY ordinal',(filing['ingest_id'],)).fetchall()
    fields=('statement','concept','label','level','parent_concept','is_breakdown','dimension_axis','dimension_member','dimension_count',
            'period_start','period_end','value','unit','balance')
    lines=[]
    for r in rows:
        x={k:r[k] for k in fields};x['is_breakdown']=bool(x['is_breakdown']);x['dimensions']=json.loads(r['dimensions_json']) if r['dimensions_json'] is not None else None;lines.append(x)
    observed={**dict(filing),'lines':lines,'native_document_set':value['document_set']}
    refs=filing.get('source_record_refs') if isinstance(filing,dict) else None
    if refs is None:refs=json.loads(filing['source_record_refs_json'])
    expected=prepare_evidence(connection,__import__('pathlib').Path(path).parent,dispatch=dispatch,request=request,
                              filing=observed,cik=filing['cik'],source_refs=refs,
                              governance_ref=filing['governance_ref'],governance_hash=filing['governance_hash'])
    if expected!=value:
        raise NativeAnnualError('native annual kept source evidence differs')
    return value
