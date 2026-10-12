"""Prove a missing unit prerequisite of the current model-input preflight.

This grants only a scheduling opportunity. Normal source authority, quota,
acquisition and exact XML/fact/financial acceptance remain unchanged.
"""
from __future__ import annotations
import json
from .store import canonical_json, content_hash
from .material_requests import normalise_need, request_digest
from .sec_filing_units import RULE_REF, unresolved_unit_refs

BASIS = 'current_model_unit_input_gap'


def unit_gap_request_fields(filing, record):
    """The published exact gap descriptor; parity with the writer is tested.

    Keep this scheduling-only projection outside sec_filing_units.py, whose
    whole-file hash versions financial normalizer snapshots. No financial
    input identity should change merely to add scheduling opportunities.
    """
    if record.get('status')!='available' or not (unresolved_unit_refs(record.get('lines')or[])or record.get('unresolved_unit_refs')):return None
    return dict(requester={'lane':'statement_context','product_ref':filing['ingest_id'],'run_ref':record['version_id'],'section':'exact_xml_units'},
        subject=filing['company_ref'],period=filing['report_date'],
        need={'kind':'document','doc_kinds':['sec_10k'if filing['form']=='10-K'else'sec_10q'],
              'question':'Obtain the original primary SEC filing '+filing['accession']+' for exact XML unit id→namespace measure/divide and full-context numeric fact proof. Opaque unit ids cannot be guessed or inherited from another accession.'},
        source_priority=['indexed_corpus','sec_edgar'],
        acceptance={'min_passages':1,'about_company':True,'date_from':filing['filed'],'date_to':filing['filed'],'document_ref':'sec:filing:'+filing['accession'],'accession':filing['accession'],'cik':filing['cik'],'unit_proof_rule_ref':RULE_REF,
                    'model_input_admission':'Strong original manifest/raw hash, exact XML unit definition, concept, CIK, dates, complete dimensions and numeric value must all replay before numeric use.'})


def unit_input_proof(connection, request, mission, *, contract_ref, cache):
    from .statement_context import _filing, _latest, context_record
    company=request['subject']
    if (company not in {m['company_ref']for m in mission.get('universe',[])}
            or request.get('digest')!=request_digest(company,request['need'],request.get('period'))
            or (request.get('acceptance')or{}).get('unit_proof_rule_ref')!=RULE_REF):
        return None
    # Exact current input identity, not a bare section label or caller's
    # alleged unit gap. Preflight currently checks all registered filings;
    # this scheduling proof deliberately does not change that scope.
    matches=[]
    for who in request.get('requesters')or[]:
        if who.get('lane')!='statement_context' or who.get('section')!='exact_xml_units':continue
        ingest=who.get('product_ref');context_ref=who.get('run_ref')
        if not ingest or not context_ref:continue
        key=(company,ingest)
        if key not in cache:
            row=connection.execute('SELECT ingest_id FROM coverage_mission_statement_filings WHERE ingest_id=? AND company_ref=?',
                                   (ingest,company)).fetchone()
            if row is None:cache[key]=None
            else:
                filing=_filing(connection,ingest)
                latest=_latest(connection,ingest)
                context=context_record(connection,latest)if latest else None
                cache[key]=(filing,context)
        held=cache[key]
        if held is None:continue
        filing,context=held
        if context is None or context['version_id']!=context_ref:continue
        fields=unit_gap_request_fields(filing,context)
        if fields is None:continue
        if (fields['subject']!=company or fields['period']!=request.get('period')
                or normalise_need(fields['need'])!=request['need']
                or fields['acceptance']!=request.get('acceptance')
                or fields['source_priority']!=request.get('source_priority')
                or fields['requester']!=who):continue
        identity={"ingest_ref":ingest,"filing_hash":filing['content_hash'],
                  "context_ref":context_ref,"context_hash":context['content_hash'],
                  "accession":filing['accession'],"cik":filing['cik'],
                  "period":filing['report_date'],"filed":filing['filed'],"form":filing['form']}
        if identity not in matches:matches.append(identity)
    if len(matches)!=1:return None
    body={"contract_ref":contract_ref,"basis":BASIS,"gap_key":"exact_xml_units",
          "request_ref":request['id'],"request_hash":request['content_hash'],
          "company_ref":company,"mission_version_ref":mission['id'],"mission_hash":mission['content_hash'],
          "spec_ref":None,"spec_hash":None,"forecast_ref":None,"forecast_hash":None,
          "model_preflight_ref":"sec-filing-units:prepare-model-unit-inputs:1",
          "unit_input":matches[0]}
    return {**body,"content_hash":content_hash(body)}


def unit_input_still_current(connection, proof):
    """Recheck immutable input pointers; source/financial checks are not skipped."""
    from .statement_context import _latest
    identity=proof['unit_input']
    row=connection.execute('SELECT company_ref,content_hash,accession,cik,report_date,filed,form FROM coverage_mission_statement_filings WHERE ingest_id=?',
                           (identity['ingest_ref'],)).fetchone()
    if row is None or tuple(row)!=(proof['company_ref'],identity['filing_hash'],identity['accession'],identity['cik'],identity['period'],identity['filed'],identity['form']):return False
    if _latest(connection,identity['ingest_ref'])!=identity['context_ref']:return False
    row=connection.execute('SELECT record_json,content_hash FROM statement_context_versions WHERE version_id=?',
                           (identity['context_ref'],)).fetchone()
    if row is None:return False
    wire=json.loads(row['record_json'])
    if (canonical_json(wire)!=row['record_json'] or row['content_hash']!=identity['context_hash']
            or wire.get('content_hash')!=row['content_hash']
            or content_hash({k:v for k,v in wire.items()if k!='content_hash'})!=row['content_hash']):return False
    # Fulfilled/changed unit contexts lose their old priority immediately.
    return wire.get('status')=='available' and unit_gap_request_fields({
        'company_ref':proof['company_ref'],'ingest_id':identity['ingest_ref'],
        'accession':identity['accession'],'cik':identity['cik'],'report_date':identity['period'],
        'filed':identity['filed'],'form':identity['form']},wire)is not None
