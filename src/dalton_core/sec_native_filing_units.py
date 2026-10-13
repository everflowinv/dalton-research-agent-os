"""Exact native annual unit proofs; no transport, taxonomy or role inference.

Traditional XBRL instances are separate from the unchanged domestic inline
parser. A unit is useful only beside a matching original numeric fact and its
complete SEC issuer, period and dimensions.
"""
from __future__ import annotations

import base64
import gzip
import hashlib
import io
import re
from datetime import date
from decimal import Decimal, InvalidOperation
from xml.etree import ElementTree as ET

from .sec_filing_units import (FilingUnitError, MAX_BYTES, XBRLI, XBRLDI,
    _expanded, _measure, match_row_units, parse_filing_units, NUMERIC_RULE_REF)
from .store import content_hash

CONTRACT = 'sec-native-annual-units:0.1'
INSTANCE_RULE = 'sec-native-xbrl-instance-units:0.1'


def parse_instance_units(raw):
    """Parse a complete original XBRL instance without translating concepts."""
    if not isinstance(raw, bytes) or len(raw) > MAX_BYTES:
        raise FilingUnitError('native unit instance exceeds the byte limit')
    if raw.startswith(b'\x1f\x8b'):
        with gzip.GzipFile(fileobj=io.BytesIO(raw)) as stream:
            body = stream.read(MAX_BYTES + 1)
    else:
        body = raw
    if len(body) > MAX_BYTES:
        raise FilingUnitError('native unit instance decompressed bytes exceed the limit')
    try:
        text = body.decode('utf-8-sig')
    except UnicodeDecodeError as exc:
        raise FilingUnitError('native unit instance is not UTF-8') from exc
    if re.search(r'<!ENTITY\b', text, re.I):
        raise FilingUnitError('native unit instance entity declarations are unsupported')
    scopes = {}; stack = []; pending = []; root = None
    try:
        for event, node in ET.iterparse(io.StringIO(text), events=('start', 'end', 'start-ns')):
            if event == 'start-ns':
                pending.append(node); continue
            if event == 'start':
                scope = dict(stack[-1]) if stack else {}
                scope.update(pending); pending.clear(); stack.append(scope); scopes[id(node)] = scope
                if root is None:root = node
                if len(stack) > 256 or len(scopes) > 500000:
                    raise FilingUnitError('native unit instance exceeds element or nesting limit')
            else:
                stack.pop()
    except ET.ParseError as exc:
        raise FilingUnitError('native unit instance is not complete valid XML') from exc
    if root is None or root.tag != '{'+XBRLI+'}xbrl':
        raise FilingUnitError('native unit source is not a complete XBRL instance')
    units = {}; contexts = {}; facts = []
    for node in root:
        fragment = ET.tostring(node, encoding='unicode')
        ref = node.get('id')
        if node.tag == '{'+XBRLI+'}unit':
            if not ref or ref in units:raise FilingUnitError('native instance repeats or omits a unit id')
            children = list(node)
            try:
                if len(children) == 1 and children[0].tag == '{'+XBRLI+'}measure':
                    canonical = _measure(children[0], scopes)
                elif len(children) == 1 and children[0].tag == '{'+XBRLI+'}divide':
                    divide = children[0]
                    numerator = divide.find('{'+XBRLI+'}unitNumerator')
                    denominator = divide.find('{'+XBRLI+'}unitDenominator')
                    if (len(divide) != 2 or numerator is None or denominator is None
                            or len(numerator) != 1 or len(denominator) != 1
                            or numerator[0].tag != '{'+XBRLI+'}measure'
                            or denominator[0].tag != '{'+XBRLI+'}measure'):
                        raise FilingUnitError('native instance divide is not currency per share')
                    currency = _measure(numerator[0], scopes); denom = _measure(denominator[0], scopes)
                    if re.fullmatch('[A-Z]{3}', currency) is None or denom != 'shares':
                        raise FilingUnitError('native instance divide is not currency per share')
                    canonical = currency+'PerShare'
                else:
                    raise FilingUnitError('native instance unit is not one measure/divide')
            except FilingUnitError:
                canonical = None
            units[ref] = {'unit_ref':ref, 'unit':canonical,
                          'definition_sha256':hashlib.sha256(fragment.encode()).hexdigest()}
        elif node.tag == '{'+XBRLI+'}context':
            if not ref or ref in contexts:raise FilingUnitError('native instance repeats or omits a context id')
            identifiers = node.findall('.//{'+XBRLI+'}identifier')
            periods = node.findall('{'+XBRLI+'}period')
            if len(identifiers) != 1 or len(periods) != 1:
                raise FilingUnitError('native instance context requires one issuer and period')
            identifier = identifiers[0]; period = periods[0]
            cik = (identifier.text or '').strip()
            if identifier.get('scheme') != 'http://www.sec.gov/CIK' or re.fullmatch(r'\d{1,10}', cik) is None:
                raise FilingUnitError('native instance context lacks exact SEC issuer')
            children = list(period)
            if len(children) == 1 and children[0].tag == '{'+XBRLI+'}instant':
                start = None; end = children[0].text
            elif len(children) == 2 and {x.tag for x in children} == {'{'+XBRLI+'}startDate', '{'+XBRLI+'}endDate'}:
                start = period.findtext('{'+XBRLI+'}startDate'); end = period.findtext('{'+XBRLI+'}endDate')
            else:
                raise FilingUnitError('native instance context period shape differs')
            if not isinstance(end, str) or date.fromisoformat(end).isoformat() != end:
                raise FilingUnitError('native instance context end date differs')
            if start is not None and (date.fromisoformat(start).isoformat() != start or start > end):
                raise FilingUnitError('native instance context duration differs')
            dims = {}; supported = True
            for child in node.iter():
                if child.tag == '{'+XBRLDI+'}typedMember':supported = False
                if child.tag == '{'+XBRLDI+'}explicitMember':
                    axis = _expanded(child.get('dimension') or '', scopes[id(child)])
                    member = _expanded(child.text or '', scopes[id(child)])
                    if axis in dims:raise FilingUnitError('native instance context repeats a dimension')
                    dims[axis] = member
            contexts[ref] = {'context_ref':ref, 'cik':cik.zfill(10), 'period_start':start,
                'period_end':end, 'dimensions':dict(sorted(dims.items())), 'supported':supported,
                'context_sha256':hashlib.sha256(fragment.encode()).hexdigest()}
    identities = {}
    for node in root:
        cref = node.get('contextRef'); uref = node.get('unitRef')
        if cref not in contexts or uref not in units or len(node):continue
        if node.get('{http://www.w3.org/2001/XMLSchema-instance}nil') in {'true', '1'}:continue
        if node.get('scale') is not None or node.get('format') is not None or node.get('sign') is not None:continue
        value = (node.text or '').strip()
        if re.fullmatch(r'[+-]?(?:\d+(?:\.\d*)?|\.\d+)(?:[eE][+-]?\d+)?', value) is None:continue
        try:amount = Decimal(value)
        except InvalidOperation:continue
        if not amount.is_finite() or (not amount.is_zero() and abs(amount.adjusted()) > 1000):continue
        key = (node.tag, cref, uref)
        if key in identities and identities[key] != amount:
            raise FilingUnitError('native instance repeats conflicting exact fact identities')
        identities[key] = amount
        facts.append({'concept':node.tag, 'context_ref':cref, 'unit_ref':uref,
            'value':format(amount, 'f'), 'fact_sha256':hashlib.sha256(ET.tostring(node, encoding='utf-8')).hexdigest()})
    return {'rule_ref':INSTANCE_RULE, 'raw_sha256':hashlib.sha256(raw).hexdigest(),
            'namespace_bindings':scopes[id(root)], 'units':units, 'contexts':contexts, 'facts':facts}


def _document_set(connection, state_dir, filing):
    from .sec_native_annual import stored_evidence, verify_document_set, compact_document_set
    from .acquired_sec_filings import _spool_bytes
    from pathlib import Path
    evidence = stored_evidence(connection, filing)
    ref = evidence['raw_parse_ref']
    if not ref.startswith('raw-sink:'):
        raise FilingUnitError('native unit source requires its exact kept parser artifact')
    raw, problem = _spool_bytes(Path(state_dir), ref[9:])
    if raw is None:raise FilingUnitError('native unit original parser artifact unavailable: '+str(problem))
    payload = __import__('json').loads(raw)
    matches = [x for x in payload.get('filings') or [] if x.get('accession') == filing['accession']]
    if len(matches) != 1:raise FilingUnitError('native unit original accession is not unique')
    value = matches[0]['native_document_set']
    checked = verify_document_set(value, issuer=str(filing['cik']).zfill(10),
        filing=value['filing'], reporting_profile_hash=value['reporting_profile_hash'])
    if compact_document_set(checked) != evidence['document_set']:
        raise FilingUnitError('native unit original set differs from immutable annual evidence')
    if any(checked['filing'][k] != filing[k] for k in ('accession', 'form', 'filed', 'report_date')):
        raise FilingUnitError('native unit original set filing binding differs')
    return evidence, checked


def _match_documents(docset, rows):
    """Use one complete original member per fact; never splice local XML ids."""
    chosen = {}; identities = {}
    declared = {m['document']:m['document_type'] for m in docset['members']}
    for member in docset['originals']:
        raw = base64.b64decode(member['base64'], validate=True)
        if hashlib.sha256(raw).hexdigest() != member['sha256'] or len(raw) != member['bytes']:
            raise FilingUnitError('native unit member bytes differ')
        if len(raw) > MAX_BYTES:raise FilingUnitError('native unit member exceeds the byte limit')
        body = raw
        if raw.startswith(b'\x1f\x8b'):
            with gzip.GzipFile(fileobj=io.BytesIO(raw)) as stream:body = stream.read(MAX_BYTES + 1)
        if len(body) > MAX_BYTES:raise FilingUnitError('native unit member decompressed bytes exceed the limit')
        try:
            _, root = next(iter(ET.iterparse(io.BytesIO(body), events=('start',))))
        except (ET.ParseError, StopIteration):
            # Non-XML cover/PDF members provide no unit authority. A detected
            # financial XML root below must pass its full parser, never fall
            # back after a contradictory or incomplete financial instance.
            if declared.get(member['document']) == 'EX-101.INS':
                raise FilingUnitError('declared native XBRL instance is not readable XML')
            continue
        if root.tag == '{'+XBRLI+'}xbrl':
            parsed = parse_instance_units(raw)
        elif declared.get(member['document']) == 'EX-101.INS':
            raise FilingUnitError('declared native XBRL instance root differs')
        elif root.tag in {'html','{http://www.w3.org/1999/xhtml}html'}:
            parsed = parse_filing_units(raw, rule_ref=NUMERIC_RULE_REF)
        else:
            continue
        for fact in parsed['facts']:
            context = parsed['contexts'][fact['context_ref']]
            unit = parsed['units'][fact['unit_ref']]['unit']
            if not context['supported'] or unit is None:continue
            key = (fact['concept'],context['cik'],context['period_start'],context['period_end'],
                   tuple(sorted(context['dimensions'].items())),fact['unit_ref'])
            value = (Decimal(fact['value']), unit)
            if key in identities and identities[key] != value:
                raise FilingUnitError('native originals disagree on an exact fact value or unit')
            identities[key] = value
        resolved, matches = match_row_units(parsed, rows, cik=docset['issuer'])
        for match in matches:
            ordinal = match['ordinal']
            bound = {**match, 'original_document':member['document'],
                     'original_document_sha256':member['sha256'], 'numeric_parse_rule_ref':parsed['rule_ref']}
            bound['content_hash'] = content_hash({k:v for k,v in bound.items() if k != 'content_hash'})
            line = {**resolved[ordinal], 'unit_definition_hash':bound['content_hash']}
            if ordinal in chosen:
                earlier, _ = chosen[ordinal]
                if earlier['unit'] != line['unit']:
                    raise FilingUnitError('native originals disagree on an exact fact unit')
                # Stable first member in the already sorted immutable set.
                continue
            chosen[ordinal] = (line, bound)
    resolved = [chosen[n][0] if n in chosen else dict(row) for n, row in enumerate(rows)]
    return resolved, [chosen[n][1] for n in sorted(chosen)]


def supplemental_native_unit_lines(connection, state_dir, filing, rows):
    from .sec_native_annual import ANNUAL_FORMS
    from .sec_filing_units import unresolved_unit_refs
    if filing['form'] not in ANNUAL_FORMS:
        raise FilingUnitError('native unit support requires an original foreign annual')
    if not unresolved_unit_refs(rows):return [dict(row) for row in rows], None
    evidence, docset = _document_set(connection, state_dir, filing)
    resolved, matches = _match_documents(docset, rows)
    if not matches:return resolved, None
    proof = {'contract_ref':CONTRACT, 'native_evidence_hash':evidence['content_hash'],
        'raw_parse_ref':evidence['raw_parse_ref'], 'document_set_hash':docset['content_hash'],
        'evidence':matches, 'evidence_hash':content_hash(matches)}
    return resolved, {**proof, 'content_hash':content_hash(proof)}


def validate_native_unit_supplement(connection, record, filing):
    proof = record.get('supplementary_native_units')
    if proof is None:return
    if (record.get('kind') != 'complete_snapshot' or record.get('native_unit_contract') != CONTRACT
            or proof.get('contract_ref') != CONTRACT
            or proof.get('content_hash') != content_hash({k:v for k,v in proof.items() if k != 'content_hash'})
            or proof.get('evidence_hash') != content_hash(proof.get('evidence'))):
        raise FilingUnitError('native unit supplement proof hash or kind differs')
    original = [dict(row) for row in record['lines']]; seen = set()
    for match in proof['evidence']:
        n = match['ordinal']
        if type(n) is not int or n < 0 or n >= len(original) or n in seen:
            raise FilingUnitError('native unit ordinal is invalid or repeated')
        seen.add(n); row = original[n]
        if (row.get('source_unit_ref') != match['source_unit_ref']
                or row.get('unit_definition_hash') != match['content_hash'] or row.get('unit') != match['unit']):
            raise FilingUnitError('native unit line binding differs')
        row['unit'] = row.pop('source_unit_ref'); row.pop('unit_definition_hash')
    from pathlib import Path
    paths = [r[2] for r in connection.execute('PRAGMA database_list').fetchall() if r[1] == 'main' and r[2]]
    if len(paths) != 1:raise FilingUnitError('native unit proof requires its existing Core location')
    lines, expected = supplemental_native_unit_lines(connection, Path(paths[0]).parent, filing, original)
    if lines != record['lines'] or expected != proof:
        raise FilingUnitError('native unit exact original/context/fact replay differs')


def rebind_companyfacts(proof, before, after, units, filing):
    """After original native replay, rebind only declared exact unit overlays."""
    from .statement_companyfacts_context import validate_supplement
    validate_supplement({'kind':'complete_snapshot', 'lines':before,
                        'supplementary_companyfacts':proof}, filing)
    if (units.get('contract_ref') != CONTRACT or units.get('content_hash') != content_hash({
            k:v for k,v in units.items() if k != 'content_hash'}) or len(before) != len(after)):
        raise FilingUnitError('native companyfacts/unit composition proof differs')
    matches = {}
    for match in units['evidence']:
        n = match['ordinal']
        if (type(n) is not int or not 0 <= n < len(before) or n in matches
                or match['content_hash'] != content_hash({k:v for k,v in match.items() if k != 'content_hash'})):
            raise FilingUnitError('native companyfacts/unit ordinal binding differs')
        if any(before[n].get(k) != match.get(k) for k in ('concept','period_start','period_end','value','dimensions')):
            raise FilingUnitError('native companyfacts/unit source fact differs')
        if before[n].get('unit') != match['source_unit_ref']:
            raise FilingUnitError('native companyfacts/unit source unit differs')
        matches[n] = match
    for n, old in enumerate(before):
        expected = dict(old)
        if n in matches:
            match = matches[n]
            expected.update(unit=match['unit'], source_unit_ref=match['source_unit_ref'],
                            unit_definition_hash=match['content_hash'])
        if after[n] != expected:
            raise FilingUnitError('native companyfacts/unit changed an unproved field')
    updated = {**proof, 'evidence':[{**item, 'line_hash':content_hash(after[item['ordinal']])}
                                  for item in proof['evidence']]}
    updated['evidence_hash'] = content_hash(updated['evidence'])
    validate_supplement({'kind':'complete_snapshot', 'lines':after,
                        'supplementary_companyfacts':updated}, filing)
    return updated


def _unit_request_descriptor(filing, context_ref):
    from .sec_native_annual import ANNUAL_FORMS
    from .sec_filing_units import RULE_REF
    if filing['form'] not in ANNUAL_FORMS:
        raise FilingUnitError('native unit request requires exact native annual form')
    if filing['governance_ref'] != 'connector-governance:sec-financial-statements:v6':
        raise FilingUnitError('native unit request requires original approved v6 filing')
    kinds = {'20-F':'sec_20f', '20-F/A':'sec_20f_amendment',
             '40-F':'sec_40f', '40-F/A':'sec_40f_amendment'}
    return dict(requester={'lane':'statement_context','product_ref':filing['ingest_id'],
                          'run_ref':context_ref,'section':'exact_xml_units'},
        subject=filing['company_ref'], period=filing['report_date'],
        need={'kind':'document', 'doc_kinds':[kinds[filing['form']]],
              'question':'Obtain the original native SEC annual document set '+filing['accession']+
                ' for exact XML unit id→namespace measure/divide and full-context numeric fact proof. Preserve original '+
                filing['form']+' and annual duration; opaque unit ids cannot be guessed or inherited from another accession.'},
        source_priority=['indexed_corpus','sec_edgar'],
        acceptance={'min_passages':1,'about_company':True,'date_from':filing['filed'],'date_to':filing['filed'],
            'document_ref':'sec:filing:'+filing['accession'],'accession':filing['accession'],'cik':filing['cik'],
            'unit_proof_rule_ref':RULE_REF,
            'model_input_admission':'Strong original native document-set/raw hash, exact XML unit definition, concept, CIK, dates, complete dimensions and numeric value must all replay before numeric use.'})


def unit_gap_request_fields(filing, record):
    from .sec_filing_units import unresolved_unit_refs
    if record.get('status') != 'available' or not (unresolved_unit_refs(record.get('lines') or []) or record.get('unresolved_unit_refs')):
        return None
    return _unit_request_descriptor(filing,record['version_id'])


def open_unit_gap_request(state_dir, filing, record):
    from .sec_native_annual import ANNUAL_FORMS
    if filing['form'] not in ANNUAL_FORMS:
        from .sec_filing_units import open_unit_gap_request as domestic
        return domestic(state_dir, filing, record)
    fields = unit_gap_request_fields(filing, record)
    if fields is None:return None
    from .material_requests import open_request
    return open_request(state_dir, **fields)


def prepare_model_unit_inputs(missions, state_dir, company_ref):
    from .sec_native_annual import ANNUAL_FORMS
    from .sec_filing_units import unresolved_unit_refs
    filings = missions.statement_filings(company_ref)
    if not any(f['form'] in ANNUAL_FORMS for f in filings):
        from .sec_filing_units import prepare_model_unit_inputs as domestic
        return domestic(missions, state_dir, company_ref)
    from .statement_context import refresh_company_contexts, _filing, _latest, context_record
    selected = [f['ingest_id'] for f in filings if unresolved_unit_refs(missions.statement_lines(f['ingest_id']))]
    if not selected:return {'refreshed':False,'unresolved':[]}
    refresh_company_contexts(missions.store,state_dir,company_ref,ingest_ids=selected,force=True)
    missing = []
    for ingest in selected:
        refs = unresolved_unit_refs(missions.statement_lines(ingest))
        if not refs:continue
        filing = _filing(missions.connection,ingest); version = _latest(missions.connection,ingest)
        request = open_unit_gap_request(state_dir,filing,context_record(missions.connection,version)) if version else None
        missing.append({'ingest_id':ingest,'accession':filing['accession'],'source_unit_refs':refs,
                        'material_request_ref':None if request is None else request['id']})
    return {'refreshed':True,'unresolved':missing}


def unit_requirement_status(state_dir, request):
    """Native material acceptance is original numeric proof, never read text."""
    kinds = {'sec_20f','sec_20f_amendment','sec_40f','sec_40f_amendment'}
    if not kinds.intersection((request.get('need') or {}).get('doc_kinds') or []):
        from .sec_filing_units import unit_requirement_status as domestic
        return domestic(state_dir,request)
    missing={'met':False,'partial':False,'items':[],
             'reason':'尚缺该外国年度原件的 exact XML 单位、完整 context 及数值证明；不据此假设季度数据。'}
    from pathlib import Path
    import sqlite3
    from contextlib import closing
    from .readonly_sqlite import connect_read_only
    from .statement_context import _filing,_latest,context_record
    from .sec_filing_units import unresolved_unit_refs
    from .material_requests import normalise_need,request_digest
    acceptance=request.get('acceptance') or {}
    if request.get('digest') != request_digest(request.get('subject'),request.get('need'),request.get('period')):
        return missing
    try:
        with closing(connect_read_only(Path(state_dir)/'core.sqlite')) as connection:
            connection.row_factory=sqlite3.Row;connection.execute('PRAGMA busy_timeout=250');connection.execute('BEGIN')
            rows=connection.execute('SELECT ingest_id FROM coverage_mission_statement_filings WHERE company_ref=? AND accession=? ORDER BY ingest_id LIMIT 17',
                                    (request.get('subject'),acceptance.get('accession'))).fetchall()
            if len(rows)>16:return missing
            for row in rows:
                filing=_filing(connection,row[0]);ref=_latest(connection,row[0])
                if ref is None:continue
                descriptor=_unit_request_descriptor(filing,ref)
                if (request.get('subject')!=descriptor['subject'] or request.get('period')!=descriptor['period']
                        or request.get('need')!=normalise_need(descriptor['need'])
                        or acceptance!=descriptor['acceptance'] or request.get('source_priority')!=descriptor['source_priority']
                        or not any(w.get('lane')=='statement_context' and w.get('product_ref')==filing['ingest_id']
                                   and w.get('section')=='exact_xml_units' for w in request.get('requesters') or [])):
                    continue
                record=context_record(connection,ref);proof=record.get('supplementary_native_units')
                if not proof or not proof.get('evidence') or unresolved_unit_refs(record.get('lines') or []):continue
                item={'document_ref':acceptance['document_ref'],'kind':descriptor['need']['doc_kinds'][0],
                    'statement_context_ref':ref,'statement_context_hash':record['content_hash'],
                    'native_unit_contract':CONTRACT,'native_unit_proof_hash':proof['content_hash'],
                    'native_source_evidence_hash':proof['native_evidence_hash'],
                    'raw_parse_ref':proof['raw_parse_ref'],'native_document_set_hash':proof['document_set_hash']}
                return {'met':True,'partial':False,'items':[item],
                        'reason':'已按正式 native 年度 document-set 原件强重验 unit/context/fact；没有推定季度或转换 IFRS 科目。'}
    except (ValueError,KeyError,TypeError,OSError,sqlite3.Error):
        return missing
    return missing
