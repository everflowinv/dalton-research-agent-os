"""Current source-replayed model discrepancy -> existing note reading choices.

This is operations feedback. It neither adopts a calculated value nor creates
a reading admission, a repair slot or permission to spend.
"""
from __future__ import annotations

import copy
import json
import re
import sqlite3
import time
from decimal import Decimal
from datetime import datetime, timezone
from pathlib import Path

from .store import canonical_json, content_hash

CONTRACT = 'model-history-note-priority:0.1'
MAX_ROOTS = 2
MAX_OPTIONS = 4
READ_SECONDS = 6
_HASH = re.compile(r'^[0-9a-f]{64}$')


def associate(*, state, spec, diagnostic, documents, root_proof, source_rows):
    """Only a typed EPS divide mismatch can select a numerator-note target."""
    from .document_research_strategy import normalize_evidence_target
    from .planner_note_options import option_ref
    if (content_hash({k:v for k,v in state.items() if k != 'state_hash'}) != state['state_hash']
            or spec['company_ref'] != state['company_ref']
            or diagnostic.get('contract_ref') != 'model-history-tie-diagnostics:0.1'
            or diagnostic.get('content_hash') != content_hash({k:v for k,v in diagnostic.items() if k!='content_hash'})
            or diagnostic.get('financial_evidence') is not False or diagnostic.get('retry_authority') is not False):
        raise ValueError('history note priority current diagnostic binding differs')
    structure = spec['financial_statement_structure']
    lines = {line['ref']:line for line in structure['lines']}
    formulas = [f for f in structure['formulas'] if f['output_ref']==diagnostic['output_ref']]
    if len(formulas)!=1:
        return None
    formula = formulas[0]; output = lines[formula['output_ref']]
    if (output['role']!='diluted_eps' or formula['operator']!='divide'
            or diagnostic['operator']!='divide' or content_hash(formula)!=diagnostic['formula_hash']
            or formula['tie_out_concept']!=diagnostic['tie_out_concept']
            or lines[formula['numerator_ref']]['role']!='diluted_eps_numerator'
            or lines[formula['denominator_ref']]['role']!='diluted_weighted_average_shares'):
        return None
    mismatches=diagnostic['mismatches']
    if not isinstance(mismatches,list) or not 0<len(mismatches)<=12:
        raise ValueError('history note priority mismatch window differs')
    periods={(x['period_start'],x['period_end']) for x in mismatches}
    options=[]
    for document in documents:
        if (document.get('readable') is not True or document.get('source_ref')!='source:sec-edgar'
                or document.get('authority_hash')!=document.get('document_version_hash')
                or not document.get('authority_ref')
                or 'search_registered_document' not in document.get('operations',())):
            continue
        for raw in document.get('evidence_targets',()):
            target=normalize_evidence_target(raw)
            if (target['kind']!='diluted_eps_numerator'
                    or document['document_ref']!='sec:filing:'+target['accession']
                    or target['accession'] not in {f['accession'] for f in state.get('filings',())}
                    or not target.get('statement_context_ref')
                    or not {(p['period_start'],p['period_end']) for p in target['periods']} <= periods):
                continue
            # Match an already admitted source and its actual period, not a
            # coincident date/amount in another filing, issuer or unit.
            cells=source_rows.get(content_hash(target),())
            eps=[c for c in cells if c.get('statement')=='income'
                and not c.get('is_breakdown') and not (c.get('dimensions') or c.get('dimension_axis')
                    or c.get('dimension_member') or c.get('dimension_count',0))
                and c.get('concept')==formula['tie_out_concept']
                and c.get('statement_context_ref')==target['statement_context_ref']
                and (c.get('period_start'),c.get('period_end')) in periods]
            def unit(value):
                return ''.join(c for c in str(value).casefold().replace('/', 'per') if c.isalnum())
            if (not eps or any(unit(c['unit'])!=unit(output['unit']) for c in eps)
                    or any(sum((c.get('period_start'),c.get('period_end'))==(p['period_start'],p['period_end']) for c in eps)!=1 for p in target['periods'])
                    or any(not any((c['period_start'],c['period_end'])==(m['period_start'],m['period_end'])
                                   and Decimal(str(c['value']))==Decimal(m['filed']) for m in mismatches) for c in eps)):
                continue
            options.append({'option_ref':option_ref(state['company_ref'],document,target),
                'document_ref':document['document_ref'], 'document_version_hash':document['document_version_hash'],
                'periods':target['periods']})
    options=sorted({o['option_ref']:o for o in options}.values(),key=canonical_json)
    if not options:
        return None
    body={'contract_ref':CONTRACT,'company_ref':state['company_ref'],'state_hash':state['state_hash'],
        'root_proof':root_proof,'output_ref':output['ref'],'role':output['role'],
        'tie_out_concept':formula['tie_out_concept'],'formula_hash':diagnostic['formula_hash'],
        'mismatches':copy.deepcopy(mismatches),'diagnostic_hash':diagnostic['content_hash'],
        'omitted_mismatch_count':diagnostic.get('omitted_mismatches',0),
        'all_mismatches_hash':diagnostic.get('all_mismatches_hash',content_hash(mismatches)),
        'note_options':options[:MAX_OPTIONS],'omitted_option_count':max(0,len(options)-MAX_OPTIONS),
        'omitted_options_hash':content_hash(options[MAX_OPTIONS:]),
        'next_action':'Read these exact EPS-note targets to establish the company-specific numerator basis; do not change filed EPS or shares to make the formula agree.',
        'financial_evidence':False,'retry_authority':False}
    return {**body,'content_hash':content_hash(body)}


def _source_rows(connection, *, company_ref, documents, periods, accessions):
    from .document_research_strategy import normalize_evidence_target
    from .company_model_annual_projection import verify_statement_filing
    from .statement_context import _latest, context_record, statement_rows
    result={}
    for document in documents:
        for raw in document.get('evidence_targets',()):
            target=normalize_evidence_target(raw)
            if (target['accession'] not in accessions or not target.get('statement_context_ref') or not
                    {(p['period_start'],p['period_end']) for p in target['periods']}<=periods):
                continue
            row=connection.execute('SELECT * FROM coverage_mission_statement_filings WHERE ingest_id=?',
                                   (target['statement_ingest_ref'],)).fetchone()
            if row is None:
                continue
            verify_statement_filing(connection,dict(row))
            if (row['company_ref']!=company_ref or row['accession']!=target['accession']
                    or row['content_hash']!=target['statement_filing_hash'] or row['form']!=target['form']
                    or _latest(connection,row['ingest_id'])!=target['statement_context_ref']
                    or context_record(connection,target['statement_context_ref'])['content_hash']!=target['statement_context_hash']):
                raise ValueError('history note priority exact current filing/context differs')
            result[content_hash(target)]=statement_rows(connection,row['ingest_id'],version_id=target['statement_context_ref'])
    return result


def _root(connection, router, budget, *, state_hash, mission):
    from .model_spec_assessment_replay import _rows, _load, _mission
    from .planner_paid_output_recovery import _router_record
    rows=_rows(connection,"json_extract(w.work_order_json,'$.metadata.model_spec_request_identity.state_hash')=? "
        "AND json_extract(w.work_order_json,'$.metadata.structured_output_repair') IS NULL",(state_hash,),MAX_ROOTS+1)
    if not rows or len(rows)>MAX_ROOTS or rows[0]['terminal_state']!='succeeded':
        return None
    work,call=_load(connection,rows[0])
    if not _mission(work,mission) or budget is None:
        return None
    route=_router_record(router,'model_route_decisions','decision_id','decision_json','decision_hash',call['route_decision_ref'])
    if (route['work_order_ref']!=work['id'] or route['work_order_hash']!=content_hash(work)
            or route['attempt_number']!=rows[0]['attempt_number'] or route['outcome']!='selected'):
        raise ValueError('history note priority original route differs')
    keys=budget.connection.execute('SELECT attempt_number,phase FROM thesis_impact_day_admissions WHERE work_order_ref=? LIMIT 65',(work['id'],)).fetchall()
    if not 0<len(keys)<=64:
        return None
    fees=[]; selected=False
    for key in keys:
        paid=budget.admission(work_order_ref=work['id'],attempt_number=key['attempt_number'],phase=key['phase'])
        if (paid is None or paid['settlement'] is None
                or (paid.get('mission_binding') or {}).get('mission_version_ref')!=mission['id']
                or (paid.get('mission_binding') or {}).get('mission_version_hash')!=mission['content_hash']
                or type(paid['settlement'].get('actual_micros')) is not int
                or paid['settlement']['actual_micros']<0):
            return None
        if key['attempt_number']==route['attempt_number'] and key['phase']=='assessment':
            if paid['admission']['route_decision_ref']!=call['route_decision_ref']:
                raise ValueError('history note priority fee route differs')
            selected=True
        fees.append({'admission_ref':paid['admission']['admission_id'],'admission_hash':paid['admission']['content_hash'],
            'settlement_ref':paid['settlement']['settlement_id'],'settlement_hash':paid['settlement']['content_hash'],
            'accounted_micros':paid['settlement']['actual_micros']})
    if not selected:
        return None
    proof={'work_ref':work['id'],'work_hash':content_hash(work),'result_hash':call['result_envelope_hash'],
        'route_ref':call['route_decision_ref'],'route_hash':route['content_hash'],'fees_hash':content_hash(fees)}
    return work,call,proof


def read(*, missions, mission, state_dir, company_ref, ticker, documents, failures):
    """Rebuild one current root; an old reason is only a cheap selection hint."""
    events=[e for e in failures if e.get('lane')=='mission_model_spec']
    if len(events)!=1 or 'does not tie to filed history' not in str(events[0].get('reason','')):
        return None
    key=events[0]['item_key'].split('|')
    if len(key)<2 or key[0]!=company_ref or _HASH.fullmatch(key[1]) is None:
        return None
    from .planner_paid_output_recovery import _authority_connection, recovery_readers
    from .company_model_state import build_company_model_state,model_spec_numeric_context_config,model_spec_prompt_byte_limit
    from .company_model_cli import filed_classifications
    from .company_model_spec import spec_from_response
    from .financial_note_context import FinancialNoteReadContext
    from .model_spec_signed_expense_repair import validate_financials_for_repair
    from .company_financial_statement_structure import FinancialStatementStructureError
    from .model_history_failure_feedback import describe_failure
    root=Path(state_dir); started=time.monotonic()
    config=json.loads((root/'initial-screen-model-config.json').read_text())
    with _authority_connection(root/'scheduler.sqlite') as sc, recovery_readers(
            config['model_router_db'],config['budget_db'],now=datetime.now(timezone.utc)) as (router,budget):
        for connection in (sc,router.connection,budget.connection if budget else None):
            if connection is not None:
                connection.execute('PRAGMA busy_timeout=250')
                connection.set_progress_handler(lambda:int(time.monotonic()-started>READ_SECONDS),2000)
        parent=_root(sc,router,budget,state_hash=key[1],mission=mission)
        if parent is None:
            return None
        work,call,proof=parent
        with FinancialNoteReadContext(store=missions.store,state_dir=root) as notes:
            state=build_company_model_state(missions,company_ref,ticker=ticker,
                industry_classification=filed_classifications(missions.store).get(company_ref),
                numeric_context_policy=model_spec_numeric_context_config(config),
                prompt_byte_limit=model_spec_prompt_byte_limit(config),financial_note_context=notes.projection(company_ref))
            if state['state_hash']!=key[1] or time.monotonic()-started>READ_SECONDS:
                return None
            resolver=lambda ref:notes.resolver(ref,expected_context=state.get('financial_note_context'))
            spec=spec_from_response(state,call['text'],decided_by=mission['autonomy']['automation_principal'],note_evidence_resolver=resolver)
            try:
                validate_financials_for_repair(missions,spec,note_evidence_resolver=resolver)
            except FinancialStatementStructureError as error:
                diagnostic,_=describe_failure(error)
                if diagnostic is None or time.monotonic()-started>READ_SECONDS:
                    return None
                sources=_source_rows(missions.connection,company_ref=company_ref,documents=documents,
                    periods={(x['period_start'],x['period_end']) for x in diagnostic['mismatches']},
                    accessions={f['accession'] for f in state['filings']})
                if time.monotonic()-started>READ_SECONDS:
                    return None
                return associate(state=state,spec=spec,diagnostic=diagnostic,documents=documents,root_proof=proof,source_rows=sources)
    return None


def signature(connection, state_dir):
    """Checked latest operations identities: a hint to rebuild, never a grant."""
    from .model_execution_failure_feedback import _event
    from .planner_paid_output_recovery import _authority_connection
    path=Path(state_dir)/'lane-failure-ledger.sqlite'
    if not path.is_file():
        return {'contract_ref':CONTRACT,'items':[]}
    started=time.monotonic()
    try:
        with _authority_connection(path) as ledger:
            ledger.execute('PRAGMA busy_timeout=250')
            ledger.set_progress_handler(lambda:int(time.monotonic()-started>1),2000)
            rows=ledger.execute("SELECT * FROM lane_failure_events WHERE lane='mission_model_spec' ORDER BY recorded_at DESC,event_id DESC LIMIT 32").fetchall()
            events=[_event(r,lane='mission_model_spec') for r in rows]
            seen=set(); items=[]
            for event in events:
                if event['item_key'] in seen:
                    continue
                seen.add(event['item_key'])
                # Repeated ticks may append the same failure with a new time.
                # That is not new research input or a new spending permission.
                items.append({k:event[k] for k in ('item_key','event','reason','failure_class')})
            return {'contract_ref':CONTRACT,'items':sorted(items,key=canonical_json)}
    except (OSError, ValueError, RuntimeError, sqlite3.Error) as error:
        return {'contract_ref':CONTRACT,'status':'unavailable','error_type':type(error).__name__}
