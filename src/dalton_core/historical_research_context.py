"""Separate dated reading context for immutable dossier/framework paragraphs."""
from __future__ import annotations
import re
from typing import Any, Mapping, Sequence
from .store import content_hash

RULE_REF='research-paragraph-historical-context:1'

def _years(period: Any) -> list[str]:
    return re.findall(r'(?<!\d)(?:19|20)\d{2}(?!\d)',str(period or ''))

def historical_findings(slots: Sequence[Mapping[str,Any]], sources: Sequence[Mapping[str,Any]],
                        material: Sequence[Mapping[str,Any]]) -> list[dict[str,Any]]:
    """An explicitly older quantitative source must be labelled with its year.

    Undated sources remain a visible gap in the reading projection; absence of
    a date is never inferred as current. This validator does not invent dates.
    """
    latest=max([year for row in material for year in _years(row.get('period'))],default=None)
    by_ref={row['ref']:row for row in sources}
    findings=[]
    for slot in slots:
        for row in slot.get('sentences') or []:
            if not re.search(r'\d',str(row.get('text') or '')):continue
            years={year for ref in row.get('refs') or [] for year in _years((by_ref.get(ref) or {}).get('period'))}
            older={year for year in years if latest and year<latest}
            if older and not all(year in row['text'] for year in older):
                findings.append({'slot_id':slot['slot_id'],'code':'historical_number_without_period',
                                 'source_years':sorted(older),'latest_material_year':latest,'text_hash':content_hash(row['text'])})
    return findings

def reading_projection(record: Mapping[str,Any], *, latest_disclosure: Mapping[str,Any] | None=None) -> dict[str,Any]:
    latest_year=str((latest_disclosure or {}).get('report_date') or '')[:4]
    blocks=list(record.get('sections') or [])
    for key in ('industry_classification','variant_view','characteristics','long_term_drivers','short_term_drivers'):
        if isinstance(record.get(key),Mapping):blocks.append({**record[key],'aspect':key})
    paragraphs=[]
    for block in blocks:
        sources={row['ref']:row for row in block.get('sources') or []}
        for slot in block.get('slots') or []:
            for ordinal,row in enumerate(slot.get('sentences') or []):
                if not re.search(r'\d',str(row.get('text') or '')):continue
                cited=[sources[ref] for ref in row.get('refs') or [] if ref in sources]
                periods=sorted({str(s['period']) for s in cited if s.get('period')})
                years={year for period in periods for year in _years(period)}
                state=('historical_comparison' if latest_year and years and max(years)<latest_year
                       else 'disclosure_period_bound' if periods else 'period_unknown')
                label={'historical_comparison':'历史对照','disclosure_period_bound':'披露期间口径','period_unknown':'期间未知，不能视为当前状态'}[state]
                paragraphs.append({'unit':block.get('aspect') or block.get('title') or block.get('link'),'slot_id':slot['slot_id'],
                                   'ordinal':ordinal,'text':row['text'],'text_hash':content_hash(row['text']),
                                   'source_refs':list(row.get('refs') or []),'source_periods':periods,
                                   'state':state,'label':label+('：'+'、'.join(periods) if periods else '')})
    body={'rule_ref':RULE_REF,'source_version_ref':record['id'],'source_hash':record['content_hash'],
          'latest_disclosure':dict(latest_disclosure) if latest_disclosure else None,'paragraphs':paragraphs,
          'original_prose_unchanged':True}
    return {**body,'content_hash':content_hash(body)}

def latest_disclosure(c: Any, company_ref: str) -> dict[str,Any] | None:
    if not c.execute("SELECT 1 FROM sqlite_master WHERE name='coverage_mission_statement_filings'").fetchone():return None
    row=c.execute('SELECT * FROM coverage_mission_statement_filings WHERE company_ref=? ORDER BY report_date DESC,filed DESC LIMIT 1',(company_ref,)).fetchone()
    if row is None:return None
    from .company_model_annual_projection import verify_statement_filing
    filing=dict(row);verify_statement_filing(c,filing,allow_legacy_metadata=True)
    from .statement_context import statement_rows
    lines=statement_rows(c,filing['ingest_id'])
    from .statement_context import context_record
    hashes={}
    identities=[];seen=set()
    for line in lines:
        dimensions=line.get('dimensions')
        if (not dimensions or line.get('period_end')!=filing['report_date'] or line.get('statement')!='income'
                or not any('segment' in str(axis).casefold() for axis in dimensions)):continue
        identity_hash=content_hash(dimensions)
        if identity_hash in seen:continue
        seen.add(identity_hash)
        ref=line.get('statement_context_ref')
        if ref and ref not in hashes:hashes[ref]=context_record(c,ref)['content_hash']
        identities.append({'dimensions':dimensions,'line_id':line['line_id'],'context_version_ref':ref,
                           'context_hash':hashes.get(ref)})
    return {**{key:filing[key] for key in ('ingest_id','content_hash','report_date','filed','accession')},
            'current_disclosed_identities':identities,'identity_status':'available' if identities else 'not_held'}
