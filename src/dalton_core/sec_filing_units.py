"""Exact inline-XBRL unit/context/fact proofs from an already verified filing.

Unit ids are opaque. Currency/share meanings come only from namespace-expanded
measure or divide definitions. This parser owns no transport or write authority.
"""
from __future__ import annotations
import gzip,hashlib,io,re
from decimal import Decimal,InvalidOperation,localcontext
from xml.etree import ElementTree as ET
from typing import Any,Mapping,Sequence
from .store import content_hash
RULE_REF='sec-filing-units:exact-definition-and-fact:1'
MAX_BYTES=32*1024*1024
XBRLI='http://www.xbrl.org/2003/instance'
XBRLDI='http://xbrl.org/2006/xbrldi'
ISO='http://www.xbrl.org/2003/iso4217'
INLINE={'http://www.xbrl.org/2008/inlineXBRL','http://www.xbrl.org/2013/inlineXBRL'}
class FilingUnitError(ValueError):pass

def _expanded(text:str,scope:Mapping[str,str])->str:
    text=text.strip();parts=text.split(':')
    if len(parts)==1:prefix,local='',parts[0]
    elif len(parts)==2:prefix,local=parts
    else:raise FilingUnitError('invalid unit/context QName')
    if prefix not in scope or not re.fullmatch(r'[A-Za-z_][\w.-]*',local):raise FilingUnitError('unbound unit/context QName')
    return '{'+scope[prefix]+'}'+local

def _measure(node,scopes):
    if node.tag!='{'+XBRLI+'}measure' or len(node):raise FilingUnitError('unit measure is not an exact XBRL measure')
    q=_expanded(node.text or '',scopes[id(node)])
    if q=='{'+XBRLI+'}shares':return 'shares'
    if q=='{'+XBRLI+'}pure':return 'pure'
    if q.startswith('{'+ISO+'}') and re.fullmatch('[A-Z]{3}',q.split('}',1)[1]):return q.split('}',1)[1]
    raise FilingUnitError('unit measure is not a supported currency/share measure')

def parse_filing_units(raw:bytes)->dict[str,Any]:
    if not isinstance(raw,bytes) or len(raw)>MAX_BYTES:raise FilingUnitError('filing unit source exceeds the byte limit')
    body=raw
    if raw.startswith(b'\x1f\x8b'):
        try:
            with gzip.GzipFile(fileobj=io.BytesIO(raw)) as f:body=f.read(MAX_BYTES+1)
        except (OSError,EOFError) as exc:raise FilingUnitError('filing unit gzip is invalid') from exc
    if len(body)>MAX_BYTES:raise FilingUnitError('filing unit decompressed source exceeds the byte limit')
    try:text=body.decode('utf8')
    except UnicodeDecodeError as exc:raise FilingUnitError('filing unit source is not UTF-8') from exc
    if re.search(r'<!ENTITY\b',text,re.I):raise FilingUnitError('filing XML entity declarations are unsupported')
    scopes={};stack=[];pending=[];nodes={'unit':[],'context':[],'fact':[]};root=None;excluded=[]
    try:
        for event,node in ET.iterparse(io.StringIO(text),events=('start','end','start-ns')):
            if event=='start-ns':pending.append(node);continue
            if event=='start':
                scope=dict(stack[-1]) if stack else {};scope.update(pending);pending.clear();stack.append(scope);scopes[id(node)]=scope
                local=node.tag.rsplit('}',1)[-1] if isinstance(node.tag,str) else ''
                blocked=(excluded[-1] if excluded else False) or local in {'script','style'};excluded.append(blocked)
                if root is None:root=node
                if not blocked:
                    if node.tag=='{'+XBRLI+'}unit':nodes['unit'].append(node)
                    elif node.tag=='{'+XBRLI+'}context':nodes['context'].append(node)
                    elif node.tag in {'{'+uri+'}nonFraction' for uri in INLINE}:nodes['fact'].append(node)
                if len(stack)>256:raise FilingUnitError('filing XML exceeds nesting limit')
                if len(scopes)>500000:raise FilingUnitError('filing XML exceeds element limit')
            else:stack.pop();excluded.pop()
    except ET.ParseError as exc:raise FilingUnitError('filing unit source is not complete valid XML') from exc
    if root is None or root.tag not in {'html','{http://www.w3.org/1999/xhtml}html'}:raise FilingUnitError('unit source is not a complete inline filing document')
    ns=scopes[id(root)];units={};contexts={};facts=[]
    for node in nodes['unit']:
        fragment=ET.tostring(node,encoding='unicode')
        if node.tag!='{'+XBRLI+'}unit':continue
        ref=node.get('id')
        if not ref or ref in units:raise FilingUnitError('filing has missing or duplicate unit ids')
        children=list(node)
        try:
            if len(children)==1 and children[0].tag=='{'+XBRLI+'}measure':canonical=_measure(children[0],scopes)
            elif len(children)==1 and children[0].tag=='{'+XBRLI+'}divide':
                divide=children[0];numerator=divide.find('{'+XBRLI+'}unitNumerator');denominator=divide.find('{'+XBRLI+'}unitDenominator')
                if len(divide)!=2 or numerator is None or denominator is None or len(numerator)!=1 or len(denominator)!=1:raise FilingUnitError('unit divide is not currency per share')
                currency=_measure(numerator[0],scopes);denom=_measure(denominator[0],scopes)
                if not re.fullmatch('[A-Z]{3}',currency) or denom!='shares':raise FilingUnitError('unit divide is not currency per share')
                canonical=currency+'PerShare'
            else:raise FilingUnitError('unit definition is not a single measure/divide')
            units[ref]={'unit_ref':ref,'unit':canonical,'definition_sha256':hashlib.sha256(fragment.encode()).hexdigest(),'definition_xml':fragment,'namespace_bindings':ns}
        except FilingUnitError:
            # Other physical units are retained as an explicit unavailable
            # definition, never assigned a currency by their id/name.
            units[ref]={'unit_ref':ref,'unit':None,'definition_sha256':hashlib.sha256(fragment.encode()).hexdigest()}
    for node in nodes['context']:
        fragment=ET.tostring(node,encoding='unicode')
        if node.tag!='{'+XBRLI+'}context':continue
        ref=node.get('id')
        if not ref or ref in contexts:raise FilingUnitError('filing has missing or duplicate context ids')
        identifier=node.find('.//{'+XBRLI+'}identifier');period=node.find('{'+XBRLI+'}period')
        if (identifier is None or period is None or identifier.get('scheme')!='http://www.sec.gov/CIK' or not re.fullmatch(r'\d{1,10}',(identifier.text or '').strip())):raise FilingUnitError('filing context lacks exact SEC entity/period')
        start=period.findtext('{'+XBRLI+'}startDate');end=period.findtext('{'+XBRLI+'}endDate');instant=period.findtext('{'+XBRLI+'}instant')
        if instant is not None:start=None;end=instant
        dims={};supported=True
        for child in node.iter():
            if child.tag=='{'+XBRLDI+'}typedMember':supported=False
            if child.tag=='{'+XBRLDI+'}explicitMember':
                axis=_expanded(child.get('dimension') or '',scopes[id(child)]);member=_expanded(child.text or '',scopes[id(child)])
                if axis in dims:raise FilingUnitError('filing context repeats a dimension axis')
                dims[axis]=member
        contexts[ref]={'context_ref':ref,'cik':(identifier.text or '').strip().zfill(10),'period_start':start,'period_end':end,'dimensions':dict(sorted(dims.items())),'supported':supported,'context_sha256':hashlib.sha256(fragment.encode()).hexdigest()}
    for node in nodes['fact']:
        try:
            fragment=ET.tostring(node,encoding='unicode')
            if node.tag.split('}')[0][1:] not in INLINE:continue
            cref=node.get('contextRef');uref=node.get('unitRef');scope=scopes[id(node)];name=_expanded(node.get('name') or '',scope)
            if cref not in contexts or uref not in units:continue
            content=''.join(node.itertext()).strip();fmt=node.get('format');scale=node.get('scale','0');sign=node.get('sign')
            if not re.fullmatch(r'-?\d{1,3}',scale) or sign not in (None,'-'):continue
            if fmt:
                expanded=_expanded(fmt,scope);uri,local=expanded[1:].split('}',1)
                if not re.fullmatch(r'https?://www\.xbrl\.org/inlineXBRL/transformation/\d{4}-\d{2}-\d{2}',uri) or local not in {'num-dot-decimal','numdotdecimal','num-comma-decimal','numcommadecimal','zerodash','zero-dash'}:continue
                if local in {'zerodash','zero-dash'}:
                    if content not in {'-','—','–'}:continue
                    content='0'
                elif local in {'num-comma-decimal','numcommadecimal'}:content=content.replace('.','').replace(',','.')
                else:content=content.replace(',','')
                content=re.sub(r'[\s\u00a0]','',content)
            if not re.fullmatch(r'[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?',content):continue
            with localcontext() as arithmetic:
                arithmetic.prec=max(64,len(content)+abs(int(scale))+10)
                value=Decimal(content).scaleb(int(scale))
            if sign=='-':value=value.copy_negate()
            if not value.is_finite() or (not value.is_zero() and abs(value.adjusted())>1000):continue
            facts.append({'concept':name,'context_ref':cref,'unit_ref':uref,'value':format(value,'f'),'fact_sha256':hashlib.sha256(fragment.encode()).hexdigest()})
        except (FilingUnitError,InvalidOperation,ValueError):continue
    return {'rule_ref':RULE_REF,'raw_sha256':hashlib.sha256(raw).hexdigest(),'namespace_bindings':ns,'units':units,'contexts':contexts,'facts':facts}

def match_row_units(parsed:Mapping[str,Any],rows:Sequence[Mapping[str,Any]],*,cik:str)->tuple[list[dict[str,Any]],list[dict[str,Any]]]:
    resolved=[];proof=[];ns=parsed['namespace_bindings']
    for ordinal,source in enumerate(rows):
        row=dict(source);ref=str(row.get('unit') or '')
        if ref not in parsed['units'] or parsed['units'][ref]['unit'] is None:resolved.append(row);continue
        try:
            concept=_expanded(row['concept'],ns);dims={_expanded(a,ns):_expanded(v,ns) for a,v in (row.get('dimensions') or {}).items()}
            if row.get('dimensions') is None:raise FilingUnitError('complete source dimensions are unavailable')
            value=Decimal(str(row['value']));matches=[]
            for fact in parsed['facts']:
                context=parsed['contexts'][fact['context_ref']]
                if (fact['concept']==concept and fact['unit_ref']==ref and Decimal(fact['value'])==value and context['supported'] and context['cik']==str(cik).zfill(10) and context['period_start']==row.get('period_start') and context['period_end']==row.get('period_end') and context['dimensions']==dims):matches.append(fact)
            if not matches:raise FilingUnitError('no exact inline fact/context/unit match')
            selected=sorted(matches,key=lambda f:(f['context_ref'],f['fact_sha256']))[0];unit=parsed['units'][ref]
            binding={'ordinal':ordinal,'concept':row['concept'],'period_start':row.get('period_start'),'period_end':row.get('period_end'),'value':str(row['value']),'dimensions':row['dimensions'],'source_unit_ref':ref,'unit':unit['unit'],'definition_sha256':unit['definition_sha256'],'context_ref':selected['context_ref'],'context_sha256':parsed['contexts'][selected['context_ref']]['context_sha256'],'fact_sha256':selected['fact_sha256']}
            binding['content_hash']=content_hash(binding);proof.append(binding);row.update(unit=unit['unit'],source_unit_ref=ref,unit_definition_hash=binding['content_hash'])
        except (FilingUnitError,InvalidOperation,KeyError,TypeError):pass
        resolved.append(row)
    return resolved,proof


def _source(connection, state_dir, filing, *, source=None):
    """Verify the original acquisition and every Core receipt, never register it anew."""
    from pathlib import Path
    from types import SimpleNamespace
    from urllib.parse import urlsplit
    from .coverage_mission import CoverageMissionAuthority
    from .acquired_sec_filings import SPOOL_ROOTS, _manifest_reader, _submissions_row, _statement_ledger_row
    from .raw_spool import RawSpoolReader, MultiRootRawSpoolReader, RawSpoolError
    from .connector_authority_port import ReadOnlyConnectorReceiptReader
    from .public_web_extraction_source import verified_public_web_source
    state=Path(state_dir)
    document='sec:filing:'+filing['accession']
    if source is None:
        rows=connection.execute('SELECT * FROM coverage_mission_discovered_documents WHERE company_ref=? AND document_ref=? AND source_ref=? AND status=? ORDER BY updated_at DESC,record_id DESC',
            (filing['company_ref'],document,'source:sec-edgar','acquired')).fetchall()
    else:
        rows=connection.execute('SELECT * FROM coverage_mission_discovered_documents WHERE record_id=?',(source['record_id'],)).fetchall()
    if not rows:raise FilingUnitError('no acquired exact filing XML source')
    failures=[]
    for row in rows:
        try:
            identity={key:row[key] for key in ('record_id','company_ref','document_ref','source_ref','mission_version_ref','ticket_ref')}
            if (row['status']!='acquired' or row['company_ref']!=filing['company_ref'] or row['document_ref']!=document or row['source_ref']!='source:sec-edgar' or not row['ticket_ref']):raise FilingUnitError('unit XML acquisition identity differs')
            authority=object.__new__(CoverageMissionAuthority);authority.connection=connection
            mission=authority._mission_record(row['mission_version_ref'])
            if filing['company_ref'] not in {item['company_ref'] for item in mission['universe']}:raise FilingUnitError('unit XML acquisition mission does not cover company')
            identity['mission_hash']=mission['content_hash']
            reader=_manifest_reader(state)
            if reader is None:raise FilingUnitError('unit XML fetch manifest is unavailable')
            manifest=reader.read_completed_manifest(row['ticket_ref'],document)
            url=urlsplit(manifest['canonical_url'])
            expected='/Archives/edgar/data/'+str(int(filing['cik']))+'/'+filing['accession'].replace('-','')+'/'
            if url.scheme!='https' or url.hostname not in {'www.sec.gov','sec.gov'} or not url.path.startswith(expected) or '/' in url.path[len(expected):] or not url.path[len(expected):]:raise FilingUnitError('unit XML source is outside exact issuer/accession EDGAR directory')
            header=_submissions_row(state,connection,row['discovery_ref'],filing['accession']) or _statement_ledger_row(connection,filing['accession'])
            if header is None or header['form']!=filing['form'] or header['filing_date']!=filing['filed']:raise FilingUnitError('unit XML original filing header differs')
            if header.get('primary_document') and url.path.rsplit('/',1)[-1]!=header['primary_document']:raise FilingUnitError('unit XML source is not the filing primary document')
            roots=[]
            for name in SPOOL_ROOTS:
                try:roots.append(RawSpoolReader(state/name))
                except RawSpoolError:pass
            spool=MultiRootRawSpoolReader(roots)
            verified,_=verified_public_web_source(SimpleNamespace(connection=connection),spool,manifest,ReadOnlyConnectorReceiptReader(connection))
            identity.update(manifest=verified,manifest_hash=verified['content_hash'])
            if source is not None and identity!=source:raise FilingUnitError('unit XML original receipt binding drifted')
            return identity,spool.read_object(verified['body_sha256'])
        except (ValueError,KeyError,TypeError,OSError) as exc:failures.append(str(exc))
    raise FilingUnitError('; '.join(failures))


def supplemental_unit_lines(connection, state_dir, filing, rows):
    """Opaque unit ids require exact definitions AND matching original numeric facts."""
    if not any(row.get('unit') and not re.fullmatch(r'[A-Z]{3}(?:PerShare)?|shares|pure|count|usd|usd_per_share|usd_per_unit',str(row['unit'])) for row in rows):return [dict(row) for row in rows],None
    try:source,raw=_source(connection,state_dir,filing)
    except (ValueError,KeyError,TypeError,OSError) as exc:
        # A missing source does not erase or canonically relabel a held old line.
        # Model-spec unit validation will retain the explicit unresolved gap.
        return [dict(row) for row in rows],None
    parsed=parse_filing_units(raw)
    lines,evidence=match_row_units(parsed,rows,cik=filing['cik'])
    if not evidence:return lines,None
    proof={'rule_ref':RULE_REF,'source':source,'raw_sha256':parsed['raw_sha256'],'evidence':evidence,'evidence_hash':content_hash(evidence)}
    return lines,proof


def validate_unit_supplement(connection, record, filing):
    proof=record.get('supplementary_units')
    if proof is None:return
    if (record.get('kind')!='complete_snapshot' or proof.get('rule_ref')!=RULE_REF or proof.get('evidence_hash')!=content_hash(proof.get('evidence'))):raise FilingUnitError('unit XML supplement proof hash or kind drifted')
    from pathlib import Path
    paths=[row[2] for row in connection.execute('PRAGMA database_list').fetchall() if row[1]=='main' and row[2]]
    if len(paths)!=1:raise FilingUnitError('unit XML proof requires existing Core file location')
    _,raw=_source(connection,Path(paths[0]).parent,filing,source=proof['source'])
    if hashlib.sha256(raw).hexdigest()!=proof.get('raw_sha256'):raise FilingUnitError('unit XML source hash drifted')
    # Undo only declared unit overlays, then replay the exact source match.
    original=[dict(row) for row in record['lines']]
    ordinals=set()
    for evidence in proof['evidence']:
        ordinal=evidence['ordinal']
        if type(ordinal) is not int or ordinal<0 or ordinal>=len(original) or ordinal in ordinals:raise FilingUnitError('unit XML proof ordinal is invalid or duplicated')
        ordinals.add(ordinal);line=original[ordinal]
        if line.get('source_unit_ref')!=evidence['source_unit_ref'] or line.get('unit_definition_hash')!=evidence['content_hash'] or line.get('unit')!=evidence['unit']:raise FilingUnitError('unit XML proof line binding drifted')
        line['unit']=line.pop('source_unit_ref');line.pop('unit_definition_hash')
    replay,evidence=match_row_units(parse_filing_units(raw),original,cik=filing['cik'])
    if replay!=record['lines'] or evidence!=proof['evidence']:raise FilingUnitError('unit XML exact fact/context/definition replay differs')


def unresolved_unit_refs(rows):
    return sorted({str(row['unit']) for row in rows if row.get('unit') and not re.fullmatch(r'[A-Z]{3}(?:PerShare)?|shares|pure|count|usd|usd_per_share|usd_per_unit',str(row['unit']))})


def open_unit_gap_request(state_dir, filing, record):
    """Append a normal exact-document request; reading/preparing never calls this."""
    if record.get('status')!='available' or not (unresolved_unit_refs(record.get('lines') or []) or record.get('unresolved_unit_refs')):return None
    from .material_requests import open_request
    document='sec:filing:'+filing['accession']
    return open_request(state_dir,
        requester={'lane':'statement_context','product_ref':filing['ingest_id'],'run_ref':record['version_id'],'section':'exact_xml_units'},
        subject=filing['company_ref'],period=filing['report_date'],
        need={'kind':'document','doc_kinds':['sec_10k' if filing['form']=='10-K' else 'sec_10q'],
              'question':'Obtain the original primary SEC filing '+filing['accession']+' for exact XML unit id→namespace measure/divide and full-context numeric fact proof. Opaque unit ids cannot be guessed or inherited from another accession.'},
        source_priority=['indexed_corpus','sec_edgar'],
        acceptance={'min_passages':1,'about_company':True,'date_from':filing['filed'],'date_to':filing['filed'],'document_ref':document,'accession':filing['accession'],'cik':filing['cik'],'unit_proof_rule_ref':RULE_REF,
                    'model_input_admission':'Strong original manifest/raw hash, exact XML unit definition, concept, CIK, dates, complete dimensions and numeric value must all replay before numeric use.'})


def unit_requirement_status(state_dir, request):
    """Exact-document material acceptance, independent of indexed text/passages."""
    from pathlib import Path
    import sqlite3
    from .statement_context import context_record, _latest
    acceptance=request.get('acceptance') or {}
    missing={'met':False,'partial':False,'items':[], 'reason':'尚缺该申报原文的 XML 单位及完整上下文事实证明；正文索引不能替代。'}
    if acceptance.get('unit_proof_rule_ref')!=RULE_REF:return missing
    accession=acceptance.get('accession');cik=acceptance.get('cik');company=request.get('subject')
    if (not accession or not cik or acceptance.get('document_ref')!='sec:filing:'+str(accession)):return missing
    path=Path(state_dir)/'core.sqlite'
    if not path.is_file():return missing
    connection=sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True);connection.row_factory=sqlite3.Row;connection.execute('PRAGMA query_only=ON')
    try:
        filings=connection.execute('SELECT ingest_id,cik FROM coverage_mission_statement_filings WHERE company_ref=? AND accession=?',(company,accession)).fetchall()
        for filing in filings:
            if str(filing['cik']).zfill(10)!=str(cik).zfill(10):continue
            ref=_latest(connection,filing['ingest_id'])
            if ref is None:continue
            record=context_record(connection,ref);proof=record.get('supplementary_units')
            if not proof or not proof.get('evidence') or unresolved_unit_refs(record.get('lines') or []):continue
            return {'met':True,'partial':False,'items':[{'document_ref':acceptance['document_ref'],'kind':((request.get('need') or {}).get('doc_kinds') or [None])[0],
                'statement_context_ref':ref,'statement_context_hash':record['content_hash'],'source_manifest_hash':proof['source']['manifest_hash'],'raw_sha256':proof['raw_sha256']}],
                'reason':'已按真实申报原文重验单位定义、完整上下文与数值，并绑定到正式快照。'}
        return missing
    except (ValueError,KeyError,TypeError,OSError,sqlite3.Error):return missing
    finally:connection.close()


def prepare_model_unit_inputs(missions, state_dir, company_ref):
    """Before any producer call, resolve known opaque units or request originals."""
    from .statement_context import refresh_company_contexts, _filing, context_record, _latest
    selected=[f['ingest_id'] for f in missions.statement_filings(company_ref)
              if unresolved_unit_refs(missions.statement_lines(f['ingest_id']))]
    if not selected:return {'refreshed':False,'unresolved':[]}
    refresh_company_contexts(missions.store,state_dir,company_ref,ingest_ids=selected,force=True)
    unresolved=[]
    for ingest in selected:
        refs=unresolved_unit_refs(missions.statement_lines(ingest))
        if not refs:continue
        filing=_filing(missions.connection,ingest);version=_latest(missions.connection,ingest)
        request=open_unit_gap_request(state_dir,filing,context_record(missions.connection,version)) if version else None
        unresolved.append({'ingest_id':ingest,'accession':filing['accession'],'source_unit_refs':refs,
            'material_request_ref':None if request is None else request['id']})
    return {'refreshed':True,'unresolved':unresolved}
