"""Keep complete latest disclosures and the exact filing groups a model consumed.

Source identity selects a group. Values never select a filing or a member.
This contract is additive: filing proofs without its marker retain their old
whole-row evaluation and bytes.
"""
from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date, timedelta
from typing import Any
from .store import content_hash

RULE_REF = 'rule:forecast-consumed-source-groups:1'


def _refuse(message):
    from .model_forecast_driver import ForecastModelValidationError
    raise ForecastModelValidationError('forecast source groups: '+message)


def _period(row):
    return (str(row.get('concept') or ''),row.get('period_start'),row.get('period_end'))


def _key(row):
    return (str(row.get('ingest_id') or ''),row.get('statement_context_ref'),*_period(row))


def _wire(rows):
    from .model_forecast_driver import _statement_line_proof_row
    return sorted([_statement_line_proof_row(r) for r in rows],key=lambda r:(str(r.get('ingest_id') or ''),int(r.get('ordinal') or 0),str(r.get('concept') or '')))


def _consumed(record, rows, filings):
    """History references only; include both cumulative operands where needed."""
    used={}
    sum_drivers={'economic:'+str(ref) for plan in (record.get('execution_contract')or{}).get('specification',{}).get('driver_formulas')or[] if plan.get('operator')=='sum' for ref in plan.get('driver_refs')or[]}
    by_accession={}
    for row in rows:
        filing=filings.get(str(row.get('ingest_id') or ''))
        if filing is None:_refuse('unknown source ingest')
        by_accession.setdefault(str(filing['accession']),[]).append(row)
    for driver in record.get('drivers') or []:
        from .period_source_family import identity_for_row
        identity=driver.get('statement_identity')
        dimensions=identity['dimensions'] if isinstance(identity,Mapping) else {}
        statement=identity['statement'] if isinstance(identity,Mapping) else driver.get('statement')
        def matches_identity(row):
            selected=identity_for_row(driver,row) if driver.get('statement_period_family_bindings') else identity
            return (selected is not None and row.get('statement')==selected['statement']
                    and (row.get('dimensions') or {})==selected['dimensions']) if identity else (
                    row.get('statement')==statement and (row.get('dimensions') or {})==dimensions)
        for cell in driver.get('history') or []:
            concept=cell.get('concept') or driver.get('concept')
            if not concept or not cell.get('accessions'):continue
            start,end=cell.get('period_start'),cell.get('period_end')
            previous=None
            if cell.get('basis')=='derived_from_cumulative':
                try:previous=(date.fromisoformat(start)-timedelta(days=1)).isoformat()
                except (TypeError,ValueError):_refuse('invalid consumed cumulative period')
            request={'driver_ref':driver['ref'],'concept':concept,'statement':statement,
                     'dimensions':dict(dimensions),'period_start':start,'period_end':end,
                     'basis':cell.get('basis'),'requires_consolidated':bool(dimensions and driver['ref']in sum_drivers),'accessions':sorted(set(map(str,cell['accessions'])))}
            matched=[]
            for accession in request['accessions']:
                candidates=[row for row in by_accession.get(accession,[]) if row.get('concept')==concept
                    and matches_identity(row)
                    and not (not dimensions and (row.get('dimension_axis') or row.get('is_breakdown')))
                    and ((row.get('period_start')==start and row.get('period_end')==end)
                         if previous is None else
                         (row.get('period_start') is not None and row['period_start']<=start
                          and row.get('period_end') in {end,previous}))]
                if not candidates:_refuse('consumed history source is missing: '+str(driver['ref'])+' / '+accession+' / '+str(end))
                matched.extend(candidates)
            for row in matched:
                group=used.setdefault(_key(row),{})
                actual_request=request
                if driver.get('statement_period_family_bindings'):
                    selected_identity=identity_for_row(driver,row)
                    if selected_identity is None:_refuse('consumed history has no explicitly selected period family')
                    actual_request={**request,'dimensions':selected_identity['dimensions']}
                group[content_hash(actual_request)]=actual_request
    return used


def _manifest(record, selected, filings, latest, consumed):
    groups={}
    for row in selected:groups.setdefault(_key(row),[]).append(row)
    entries=[]
    for key,rows in sorted(groups.items(),key=lambda item:str(item[0])):
        ingest,context,concept,start,end=key;filing=filings[ingest]
        entries.append({'ingest_id':ingest,'statement_context_ref':context,'concept':concept,
            'period_start':start,'period_end':end,'accession':str(filing['accession']),
            'filed':str(filing.get('filed') or ''),'form':filing.get('form'),
            'roles':(["latest"]if key in latest else [])+(["consumed"]if key in consumed else []),
            'statement_line_refs':sorted(str(row['line_id'])for row in rows),
            'source_rows_hash':content_hash({'statement_rows':_wire(rows)}),
            'consumed_by':[consumed[key][h]for h in sorted(consumed.get(key,{}))]})
    body={'rule_ref':RULE_REF,'company_ref':record['company_ref'],'groups':entries}
    return {**body,'content_hash':content_hash(body)}


def select_rows(record: Mapping[str,Any], filing_records: Sequence[Mapping[str,Any]],
                rows_by_ingest: Mapping[str,Sequence[Mapping[str,Any]]]):
    """Preserve the previous newest-per-concept/period order, plus consumed groups."""
    filings={str(f['ingest_id']):f for f in filing_records}
    if any(f.get('company_ref')!=record['company_ref']for f in filings.values()):_refuse('foreign company filing')
    all_rows=[];chosen={};groups={}
    for filing in filing_records:
        ingest=str(filing['ingest_id']);by_period={}
        for row in rows_by_ingest[ingest]:
            if str(row.get('ingest_id'))!=ingest:_refuse('row differs from source ingest')
            all_rows.append(row);groups.setdefault(_key(row),[]).append(row)
            by_period.setdefault(_period(row),[]).append(row)
        for period,rows in by_period.items():
            filed=str(filing.get('filed')or'')
            if period not in chosen or filed>chosen[period][0]:chosen[period]=(filed,rows)
    latest={_key(row)for _,rows in chosen.values()for row in rows}
    consumed=_consumed(record,all_rows,filings)
    keys=latest|set(consumed)
    for key,requests in consumed.items():
        if any(r['requires_consolidated'] and not any(
            row.get('statement') == r['statement'] and not row.get('dimensions')
            and not row.get('dimension_axis') and not row.get('is_breakdown')
            for row in groups[key]) for r in requests.values()):
            _refuse('consumed partition source lacks its own consolidated total: '+str(key))
    selected=[row for row in all_rows if _key(row)in keys]
    if len({str(r.get('line_id'))for r in selected})!=len(selected):_refuse('source line refs are not unique')
    return selected,_manifest(record,selected,filings,latest,consumed)


def select_from_authority(record,connection):
    from .coverage_mission import CoverageMissionAuthority
    authority=object.__new__(CoverageMissionAuthority);authority.connection=connection
    filings=authority.statement_filings(record['company_ref'])
    rows={str(f['ingest_id']):authority.statement_lines(f['ingest_id'])for f in filings}
    return select_rows(record,filings,rows)


def replay_manifest(record,rows,manifest,connection):
    """The published snapshot stays frozen; exact full context groups must replay."""
    if not isinstance(manifest,Mapping) or set(manifest)!={'rule_ref','company_ref','groups','content_hash'}:
        _refuse('source proof fields differ')
    if manifest['rule_ref']!=RULE_REF or manifest['company_ref']!=record['company_ref'] or manifest['content_hash']!=content_hash({k:v for k,v in manifest.items()if k!='content_hash'}):
        _refuse('source proof identity/hash differs')
    from .statement_context import statement_rows
    filings={};contexts={};groups={}
    for row in rows:groups.setdefault(_key(row),[]).append(row)
    if len(groups)!=len(manifest['groups']):_refuse('source proof group set differs')
    for ingest in sorted({str(row['ingest_id'])for row in rows}):
        filing=connection.execute('SELECT * FROM coverage_mission_statement_filings WHERE ingest_id=?',(ingest,)).fetchone()
        if filing is None:_refuse('source filing is missing')
        filings[ingest]=dict(filing)
    latest=set();consumed=_consumed(record,rows,filings)
    for entry in manifest['groups']:
        key=(entry['ingest_id'],entry['statement_context_ref'],entry['concept'],entry['period_start'],entry['period_end'])
        if key not in groups:_refuse('source proof group is missing')
        ingest,context=key[:2]
        if ingest not in filings:
            row=connection.execute('SELECT * FROM coverage_mission_statement_filings WHERE ingest_id=?',(ingest,)).fetchone()
            if row is None:_refuse('source filing is missing')
            filings[ingest]=dict(row)
        if filings[ingest]['company_ref']!=record['company_ref']:_refuse('foreign company filing')
        ck=(ingest,context)
        if ck not in contexts:contexts[ck]=statement_rows(connection,ingest,version_id=context,latest=context is not None)
        complete=[r for r in contexts[ck]if _key(r)==key]
        if _wire(complete)!=_wire(groups[key]):_refuse('source group is incomplete or changed')
        if 'latest'in entry['roles']:latest.add(key)
    # latest is the publisher-verified historical snapshot, not today's newer
    # filings. Re-reading an immutable proof cannot move its source selection.
    replay=_manifest(record,rows,filings,latest,consumed)
    if replay!=dict(manifest):_refuse('source roles/context proof does not replay')
    return replay


def evaluate(record,*,statement_rows=(),solver_results=()):
    """All existing checks; only segment arithmetic is isolated by exact filing/context."""
    from .economic_invariants import forecast_subject,segment_groups,evaluate as check,FORECAST_MODEL
    subject=forecast_subject(record,statement_rows=statement_rows,solver_results=solver_results)
    groups={}
    for row in statement_rows:groups.setdefault((row.get('ingest_id'),row.get('statement_context_ref'),row.get('statement')),[]).append(row)
    subject['segments']=[segment for key in sorted(groups,key=str)for segment in segment_groups(groups[key])]
    return check(output_kind=FORECAST_MODEL,output_ref=str(record.get('id')or record.get('model_ref')or'forecast-model'),company_ref=str(record.get('company_ref')),subject=subject)
