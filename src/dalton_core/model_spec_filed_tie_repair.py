"""One ordinary structural correction for an explicitly unreported subtotal.

The model may rebuild its structure from the same original authorities. Code
does not pick a tie, add a balancing amount, or enlarge the original quota.
"""
from __future__ import annotations

import copy
from collections.abc import Mapping
from .store import canonical_json, content_hash

CONTRACT_REF = 'model-spec-missing-filed-tie-structure:0.1'
ERROR_CODE = 'structure_missing_filed_tie'
DIAGNOSTIC_REF = 'financial-structure-missing-filed-tie:0.1'
MAX_CHOICES = 64


def proof(body, state, diagnostic):
    from .company_financial_statement_structure import _presentation_filed, _units, _STRUCTURE_AUTHORITY_REFS
    from .company_model_inputs import _declared_statement_hints
    from .company_model_spec import OUTPUT_SCHEMA
    from .model_spec_complete_diagnostics import _shape, _closed_refs
    schema=copy.deepcopy(OUTPUT_SCHEMA)
    # Accept known structure versions only when the complete response also fits
    # the current closed shape. Do not relabel or fill missing legacy fields.
    schema['properties']['financial_statement_structure']['properties']['schema_version'] = {
        'enum':list(_STRUCTURE_AUTHORITY_REFS)}
    if (not isinstance(diagnostic,Mapping) or diagnostic.get('contract_ref')!=DIAGNOSTIC_REF
            or set(diagnostic)!={'contract_ref','formula_index','output_ref','output_line','content_hash'}
            or diagnostic.get('content_hash')!=content_hash({k:v for k,v in diagnostic.items() if k!='content_hash'})
            or content_hash({k:v for k,v in state.items() if k!='state_hash'})!=state.get('state_hash')
            or not _shape(body,schema) or not _closed_refs(body)):
        return None
    if (not isinstance(diagnostic['output_line'],Mapping)
            or set(diagnostic['output_line'])!={'ref','role','statement','unit','period_kind'}):
        return None
    structure=body['financial_statement_structure']; index=diagnostic['formula_index']
    if type(index) is not int or not 0<=index<len(structure['formulas']):
        return None
    formula=structure['formulas'][index]
    lines=[l for l in structure['lines'] if l['ref']==diagnostic['output_ref']]
    normalized_line = (None if len(lines)!=1 else
        {k:lines[0][k] for k in ('ref','role','statement','unit','period_kind')})
    if normalized_line is not None:
        # _normalize_line applies this same normalization to the schema-checked
        # unit. Preserve the original response bytes; do not rename currencies.
        normalized_line['unit']=normalized_line['unit'].strip().casefold()
    if (len(lines)!=1 or formula['output_ref']!=diagnostic['output_ref']
            or formula.get('tie_out_concept') is not None or lines[0]['kind']!='derived'
            or normalized_line!=diagnostic['output_line']):
        return None
    filed,_=_presentation_filed(state,statement_hints=_declared_statement_hints(
        {'financial_statement_structure':structure}))
    line=normalized_line; choices=[]
    for concept, source in sorted(filed.items()):
        if (source['status']=='filed' and source['statement']==line['statement']
                and source['period_basis']==line['period_kind'] and _units(source,include_duration=True)=={line['unit']}):
            choices.append({'concept':concept,'statement':source['statement'],
                            'period_kind':source['period_basis'],'unit':line['unit']})
    if not choices or len(choices)>MAX_CHOICES:
        return None  # No partial catalog is presented as exhaustive authority.
    source_rows=[copy.deepcopy(r) for r in state.get('statements',{}).get(line['statement'],())
                 if not r.get('is_breakdown') and not (r.get('dimension_axis') or r.get('dimensions'))]
    numeric=state.get('numeric_context') or {}
    cells=[{k:c[k] for k in ('concept','period_start','period_end','period_shape','value','unit','accession',
            'line_id','line_content_hash','filing_content_hash','statement_context_ref') if k in c}
           for c in numeric.get('cells',()) if c.get('status')=='filed' and c.get('statement')==line['statement']
           and not (c.get('dimension_axis') or c.get('dimension_member') or c.get('dimensions'))]
    result={'contract_ref':CONTRACT_REF,'code':ERROR_CODE,'parent_body_hash':content_hash(body),
        'company_ref':state['company_ref'],'state_hash':state['state_hash'],
        'diagnostic':copy.deepcopy(dict(diagnostic)),
        'source_choices':choices,'source_choices_hash':content_hash(choices),
        'original_statement_rows':source_rows,'original_numeric_cells':cells,
        'original_numeric_scope':{k:numeric[k] for k in ('content_hash','included_cells','available_cells',
            'omitted_by_series_limit','omitted_by_total_limit','omitted_by_prompt_limit') if k in numeric},
        'allowed_field':'financial_statement_structure','financial_evidence':False,
        'retry_authority':'original_unused_ordinary_slot_only'}
    return {**result,'content_hash':content_hash(result)}


def validate_parent(body, held, *, state=None):
    if (not isinstance(held,Mapping) or held.get('contract_ref')!=CONTRACT_REF
            or held.get('code')!=ERROR_CODE or held.get('allowed_field')!='financial_statement_structure'
            or held.get('parent_body_hash')!=content_hash(body)
            or held.get('content_hash')!=content_hash({k:v for k,v in held.items() if k!='content_hash'})
            or held.get('source_choices_hash')!=content_hash(held.get('source_choices'))):
        raise ValueError('missing filed tie repair parent proof differs')
    if state is not None and proof(body,state,held['diagnostic'])!=dict(held):
        raise ValueError('missing filed tie repair current source/state differs')


def assert_only_structure_changed(original, repaired, held):
    from .company_model_spec import CompanyModelSpecError
    validate_parent(original,held)
    if (not isinstance(repaired,Mapping) or set(original)!=set(repaired)
            or canonical_json({k:v for k,v in original.items() if k!='financial_statement_structure'})
            !=canonical_json({k:v for k,v in repaired.items() if k!='financial_statement_structure'})):
        raise CompanyModelSpecError('missing filed tie correction changed fields outside the financial statement structure')
    if canonical_json(original['financial_statement_structure'])==canonical_json(repaired['financial_statement_structure']):
        raise CompanyModelSpecError('missing filed tie correction did not change its refused structure')
