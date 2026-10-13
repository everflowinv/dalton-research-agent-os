"""An exact latest cash window can disprove a ratio method, not its facts.

Only zero/mixed denominators with complete source operands admit the existing
one repair. Missing periods, units, durations or sources remain whole refusals.
"""
from __future__ import annotations
from copy import deepcopy
from datetime import date, timedelta
from decimal import Decimal
from .store import canonical_json, content_hash
from .model_forecast_driver import ForecastModelUnavailable

RULE_REF = 'cash-flow-ratio-applicability:0.1'
ERROR_CODE = 'cash_flow_ratio_inapplicable'


class CashRatioUnavailable(ForecastModelUnavailable):
    def __init__(self, diagnostic):
        self.diagnostic = diagnostic
        label={'zero_denominator':'zero denominator','mixed_sign_denominator':'mixed-sign denominators'}[diagnostic['reason']]
        super().__init__('cash ratio method inapplicable: '+label+'; '+canonical_json(diagnostic))


def window(cash_cells, values, refs, drivers, *, unit=None):
    """All source/duration gates precede sign classification; no amount changes."""
    latest = sorted(cash_cells, key=lambda c:str(c.get('period_end')))[-4:]
    ends = [str(c.get('period_end')) for c in latest]
    result = {'rule_ref':RULE_REF, 'target_periods':ends, 'missing_periods':[],
              'reason':None, 'samples':[]}
    if len(latest) != 4:
        result['reason']='missing_periods'; return result
    held = {}
    for d in drivers:
        for c in d.get('history') or []:
            for accession in c.get('accessions') or []:
                held.setdefault((str(c.get('concept')), str(c.get('period_end')), str(accession)), []).append((d,c))
    for prior,current in zip(latest,latest[1:]):
        try:
            continuous = date.fromisoformat(str(current['period_start'])) == date.fromisoformat(str(prior['period_end']))+timedelta(days=1)
        except (ValueError,KeyError,TypeError):continuous=False
        if not continuous: result['reason']='duration_mismatch'; return result
    for cash in latest:
        end=str(cash['period_end']); amount=values.get(end); operands=refs.get(end) or []
        if amount is None or not operands:
            result['missing_periods'].append(end); continue
        if unit is not None and cash.get('unit') is not None and str(cash['unit']).casefold()!=unit:
            result['reason']='unit_mismatch'; return result
        if not cash.get('period_start') or not cash.get('accessions'):
            result['reason']='source_unproved'; return result
        for ref in operands:
            rows=held.get((str(ref.get('concept')),end,str(ref.get('accession'))),[])
            if ref.get('kind')!='input_cell' or not ref.get('accession') or str(ref.get('period_end'))!=end or not rows:
                result['reason']='source_unproved'; return result
            if unit is not None and any(str(d.get('unit') or '').casefold()!=unit for d,_ in rows):
                result['reason']='unit_mismatch'; return result
            if any(c.get('period_start')!=cash['period_start'] for _,c in rows):
                result['reason']='duration_mismatch'; return result
        try:
            amount=Decimal(str(amount)); cash_amount=Decimal(str(cash['value']))
            if not amount.is_finite() or not cash_amount.is_finite():raise ValueError()
        except (ValueError,ArithmeticError):result['reason']='source_unproved'; return result
        result['samples'].append({'period_start':cash['period_start'],'period_end':end,
            'denominator':str(amount),'cash_value':str(cash_amount),'cash_accessions':list(cash['accessions']),
            'operand_refs':deepcopy(list(operands))})
    if result['missing_periods']:result['reason']='missing_periods'
    elif any(Decimal(c['denominator'])==0 for c in result['samples']):result['reason']='zero_denominator'
    elif len({Decimal(c['denominator'])>0 for c in result['samples']})!=1:result['reason']='mixed_sign_denominator'
    return result


def refuse_if_inapplicable(cash_cells, values, refs, drivers, *, role, base_ref, input_hash, unit=None):
    diagnostic=window(cash_cells,values,refs,drivers,unit=unit)
    if diagnostic['reason'] in {'zero_denominator','mixed_sign_denominator'}:
        raise CashRatioUnavailable({**diagnostic,'role':role,'base_ref':base_ref,'financial_input_hash':input_hash})
    return diagnostic


def repair_error(spec, inputs, diagnostic, *, note_evidence_resolver=None):
    from .company_model_spec import CompanyModelSpecError
    from .company_financial_statement_structure import materialize_financial_statement_structure
    from .model_forecast_driver import (build_cash_flow_companion, build_structure_drivers,
        _structure_historical_values, build_cash_flow_companion_drivers, quarterly_history,
        build_structured_forecast_model, _structure_result_refs)
    if diagnostic.get('role')!='operating_cash_flow':return None
    structure,_=materialize_financial_statement_structure(spec,inputs,note_evidence_resolver=note_evidence_resolver)
    try:build_cash_flow_companion(spec,inputs,structure)
    except CashRatioUnavailable as exc:
        if exc.diagnostic!=diagnostic:return None
    else:return None
    original=spec['cash_flow_companion']['lines']; positions=[i for i,l in enumerate(original)if l['role']=='operating_cash_flow']
    if len(positions)!=1 or len(structure.get('lines') or [])>64:return None
    index=positions[0]; allowed=[]
    drivers=build_structure_drivers(inputs,structure)
    values,refs=_structure_historical_values(drivers,structure)
    # The complete current cash builder must prove each choice, not its name.
    for line in structure.get('lines') or []:
        if line.get('statement')!='income' or line.get('period_kind')!='duration':continue
        from .cash_flow_derived_base import derived_base_is_forecastable
        if line.get('kind')=='filed' and line.get('forecast_method')not in {'quarterly_growth','year_over_year_growth','seasonal_factor','share_of_line'}:continue
        if line.get('kind')=='derived' and not derived_base_is_forecastable(structure,str(line.get('ref'))):continue
        ref=line.get('ref')
        if ref==original[index]['forecast_base_ref']:continue
        candidate=deepcopy(spec);candidate['cash_flow_companion']['lines'][index]['forecast_base_ref']=ref
        try:
            companion=build_cash_flow_companion(candidate,inputs,structure)
            cash=next(d for d in build_cash_flow_companion_drivers(inputs,companion)if d['role']=='operating_cash_flow')
            # A filed-base builder predates the stricter derived contract: prove
            # its latest complete window separately, never accept a legal name.
            if line.get('kind')=='filed':
                source=next(s for s in inputs['filed_lines']if s['concept']==line['concept'])
                from .cash_flow_filed_tie_base import _source_history
                _source_history(source,str(line['unit']).casefold())
            if window(quarterly_history(cash),values.get(ref,{}),refs.get(ref,{}),drivers,
                      unit=str(line['unit']).casefold())['reason'] is not None:continue
            # A permitted method and a complete history do not by themselves
            # prove future execution (e.g. growth over negative levels).
            candidate['content_hash']=content_hash({k:v for k,v in candidate.items()if k!='content_hash'})
            current,replay=materialize_financial_statement_structure(candidate,inputs,note_evidence_resolver=note_evidence_resolver)
            from .company_financial_statement_structure import forecast_structure_binding
            body=build_structured_forecast_model(candidate,inputs,structure=current,replay=replay,
                binding=forecast_structure_binding(current,replay,inputs))
            base_result=next(r for r in body['results']if r['ref']==_structure_result_refs(current)[ref])
            if (not base_result.get('cells') or any(c.get('status')!='computed' or c.get('value')is None
                    for c in base_result['cells'])):continue
        except (ValueError,ForecastModelUnavailable,StopIteration):continue
        allowed.append(str(ref))
    body={'rule_ref':RULE_REF,'code':ERROR_CODE,'company_ref':spec['company_ref'],
        'state_hash':spec['state_hash'],'spec_hash':spec['content_hash'],'financial_input_hash':content_hash(inputs),
        'line_index':index,'original_line':deepcopy(original[index]),'diagnostic':deepcopy(diagnostic),
        'allowed_base_refs':sorted(allowed),'allowed_fields':['forecast_method','forecast_base_ref','because']}
    proof={**body,'content_hash':content_hash(body)}
    return CompanyModelSpecError(canonical_json(proof),code=ERROR_CODE,proof=proof)


def validate_parent(raw, proof):
    from .company_model_spec import CompanyModelSpecError
    try:
        if set(proof)!={'rule_ref','code','company_ref','state_hash','spec_hash','financial_input_hash','line_index','original_line','diagnostic','allowed_base_refs','allowed_fields','content_hash'}:raise ValueError()
        if proof['allowed_fields']!=['forecast_method','forecast_base_ref','because']:raise ValueError()
        if (proof['rule_ref']!=RULE_REF or proof['code']!=ERROR_CODE
                or content_hash({k:v for k,v in proof.items()if k!='content_hash'})!=proof['content_hash']):raise ValueError()
        line=raw['cash_flow_companion']['lines'][proof['line_index']]
        if line!=proof['original_line'] or line['role']!='operating_cash_flow' or line['forecast_method']!='share_of_line':raise ValueError()
        if proof['diagnostic']['reason'] not in {'zero_denominator','mixed_sign_denominator'} or proof['diagnostic']['missing_periods']:raise ValueError()
        if len(proof['diagnostic']['samples'])!=4:raise ValueError()
    except (KeyError,TypeError,IndexError,ValueError) as exc:
        raise CompanyModelSpecError('cash ratio repair parent proof differs',code=ERROR_CODE) from exc


def assert_only_allowed_changed(original, repaired, proof):
    from .company_model_spec import CompanyModelSpecError
    validate_parent(original,proof)
    try:
        index=proof['line_index'];old=original['cash_flow_companion']['lines'][index];new=repaired['cash_flow_companion']['lines'][index]
        if set(old)!=set(new) or not isinstance(new['because'],str) or not new['because'].strip():raise ValueError()
        if (new['forecast_method'],new['forecast_base_ref'])!=('unavailable',None) and not (
                new['forecast_method']=='share_of_line' and new['forecast_base_ref']in proof['allowed_base_refs']):raise ValueError()
        restored=deepcopy(repaired)
        for key in proof['allowed_fields']:restored['cash_flow_companion']['lines'][index][key]=old[key]
        if canonical_json(restored)!=canonical_json(original):raise ValueError()
    except (KeyError,IndexError,TypeError,ValueError) as exc:
        raise CompanyModelSpecError('cash ratio repair changed fields outside its exact scope',code=ERROR_CODE) from exc
