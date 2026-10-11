"""One source-proven filed income-sum coefficient may be reconsidered once.

This produces a typed repair proof, never a corrected persisted specification.
The existing repair budget and complete financial validator remain authoritative.
"""
from __future__ import annotations
import copy
from decimal import Decimal
from typing import Any, Mapping
from .company_financial_statement_structure import (
    FinancialStatementStructureError, _period_cells,
)
from .company_model_spec import CompanyModelSpecError
from .store import canonical_json, content_hash

CONTRACT_REF = 'model-spec-signed-filed-income-sum-coefficient:0.1'
ERROR_CODE = 'signed_filed_income_sum_coefficient'
TERM_ROLES = frozenset({
    'cost_of_revenue','operating_expense','other_operating_income_expense',
    'interest_income','interest_expense','other_nonoperating_income_expense',
    'nonoperating_income_expense','income_tax_expense','company_presented_component',
    'income_from_continuing_operations','discontinued_operations','noncontrolling_interest',
})
OUTPUT_ROLES = frozenset({'gross_profit','operating_income','nonoperating_income_expense',
    'pretax_income','net_income','parent_net_income','income_from_continuing_operations',
    'company_presented_subtotal'})

class SignedExpenseRepairError(CompanyModelSpecError):
    def __init__(self, proof):
        self.proof = proof
        super().__init__(canonical_json(proof), code=ERROR_CODE)


def _recognized_tie_failure(spec, message):
    definition=spec.get('financial_statement_structure')
    if not isinstance(definition,Mapping):return False
    raw_lines=definition.get('lines');formulas=definition.get('formulas')
    if not isinstance(raw_lines,list) or not isinstance(formulas,list):return False
    lines={r.get('ref'):r for r in raw_lines if isinstance(r,Mapping)}
    matches=[f for f in formulas if isinstance(f,Mapping) and f.get('operator')=='sum'
        and lines.get(f.get('output_ref'),{}).get('role') in OUTPUT_ROLES
        and lines.get(f.get('output_ref'),{}).get('statement')=='income'
        and message==f"formula {f.get('output_ref')} does not tie to filed history"]
    return len(matches)==1


def repair_proof(spec: Mapping[str, Any], inputs: Mapping[str, Any],
                 failure_message: str, *, note_evidence_resolver=None) -> dict[str, Any] | None:
    if not _recognized_tie_failure(spec,failure_message):return None
    definition = spec.get('financial_statement_structure') or {}
    lines = {r['ref']: r for r in definition.get('lines', [])}
    filed = {r['concept']: r for r in inputs.get('filed_lines', [])}
    candidates=[]
    # Derive the original DAG values without changing a filed fact. The actual
    # authority validator has already checked its topology, roles and units.
    values={r['ref']:_period_cells(filed[r['concept']]) for r in lines.values()
            if r.get('kind')=='filed' and r.get('concept') in filed}
    pending=list(definition.get('formulas',[]))
    while pending:
        progress=False
        for formula in list(pending):
            dependencies=([t['line_ref'] for t in formula['terms']] if formula['operator']=='sum'
                          else [formula['numerator_ref'],formula['denominator_ref']])
            if any(ref not in values for ref in dependencies):continue
            periods=set(values[dependencies[0]])
            for ref in dependencies[1:]:periods &=set(values[ref])
            calculated={}
            for period in periods:
                if formula['operator']=='sum':
                    calculated[period]=sum((Decimal(t['coefficient'])*values[t['line_ref']][period]
                                           for t in formula['terms']),Decimal(0))
                elif values[formula['denominator_ref']][period]!=0:
                    calculated[period]=values[formula['numerator_ref']][period]/values[formula['denominator_ref']][period]
            values[formula['output_ref']]=calculated;pending.remove(formula);progress=True
        if not progress:return None
    for fi, formula in enumerate(definition.get('formulas', [])):
        output=lines.get(formula.get('output_ref'), {})
        if (formula.get('operator') != 'sum' or output.get('role') not in OUTPUT_ROLES
                or output.get('statement') != 'income'
                or failure_message != f"formula {formula.get('output_ref')} does not tie to filed history"):
            continue
        target=filed.get(formula.get('tie_out_concept'), {})
        if (target.get('status') != 'filed' or target.get('period_basis') != 'duration'):
            continue
        tied=_period_cells(target);periods=set(tied)
        for term in formula['terms']:periods &=set(values.get(term['line_ref'],{}))
        if not periods:continue
        for ti,term in enumerate(formula['terms']):
            line=lines[term['line_ref']];source=filed.get(line.get('concept'),{})
            if (line.get('kind')!='filed' or line.get('role') not in TERM_ROLES
                    or source.get('status')!='filed' or source.get('statement')!='income'
                    or source.get('period_basis')!='duration' or term.get('coefficient') not in {'-1','1'}
                    or any(not cell.get('source_accessions') for cell in source.get('cells',{}).values())):
                continue
            old=Decimal(term['coefficient']);new=-old;reports=[];changed=False
            for period in sorted(periods,key=lambda p:(p[1],p[0] or '')):
                actual=values[formula['output_ref']][period]
                corrected=actual+(new-old)*values[term['line_ref']][period]
                if corrected !=tied[period]:break
                changed=changed or actual!=tied[period]
                reports.append({'period_start':period[0],'period_end':period[1],
                    'actual':str(actual),'expected':str(tied[period]),
                    'terms':[{'line_ref':t['line_ref'],'concept':lines[t['line_ref']].get('concept'),
                              'coefficient':t['coefficient'],'value':str(values[t['line_ref']][period])}for t in formula['terms']]})
            if len(reports)!=len(periods) or not changed:continue
            # This candidate is an offline proof only. Every formula, EPS tie,
            # method/source gate and selected cash-flow companion is rechecked.
            corrected_spec=copy.deepcopy(spec)
            corrected_spec['financial_statement_structure']['formulas'][fi]['terms'][ti]['coefficient']=str(int(new))
            corrected_spec.pop('content_hash',None)
            spec_id=corrected_spec.pop('spec_id',None)
            corrected_spec['content_hash']=content_hash(corrected_spec)
            corrected_spec['spec_id']=spec_id or inputs.get('spec_ref')
            try:
                from .company_financial_statement_structure import materialize_financial_statement_structure
                from .model_forecast_driver import build_cash_flow_companion, ForecastModelError
                structure,replay=materialize_financial_statement_structure(corrected_spec,inputs,
                    note_evidence_resolver=note_evidence_resolver)
                if any(r['status']!='validated' or not r['tested_periods'] for r in replay['formulas']):continue
                if corrected_spec.get('schema_version')=='0.4':
                    build_cash_flow_companion(corrected_spec,inputs,structure)
            except (FinancialStatementStructureError,ForecastModelError,ValueError,KeyError,ArithmeticError):
                continue
            candidates.append({'contract_ref':CONTRACT_REF,'spec_hash':spec.get('content_hash'),
                'financial_inputs_hash':content_hash(inputs),'output_ref':formula['output_ref'],
                'tie_out_concept':formula['tie_out_concept'],
                'coefficient_path':['financial_statement_structure','formulas',fi,'terms',ti,'coefficient'],
                'line_ref':term['line_ref'],'concept':line['concept'],
                'old_coefficient':str(int(old)),'required_coefficient':str(int(new)),
                'evidence_refs':formula['evidence_refs'],'periods':reports})
    if len(candidates)!=1:return None
    proof=candidates[0];proof['content_hash']=content_hash(proof);return proof


def validate_coefficient_parent(original, proof):
    try:
        _,_,fi,_,ti,_=proof['coefficient_path']
        formula=original['financial_statement_structure']['formulas'][fi]
        term=formula['terms'][ti]
        leaves=[r for r in original['financial_statement_structure']['lines']
                if r.get('ref')==proof['line_ref']]
        if (formula['output_ref']!=proof['output_ref']
                or formula['tie_out_concept']!=proof['tie_out_concept']
                or term['line_ref']!=proof['line_ref']
                or type(term['coefficient']) is not str
                or term['coefficient']!=proof['old_coefficient']
                or len(leaves)!=1 or leaves[0]['concept']!=proof['concept']):raise ValueError()
    except (KeyError,IndexError,TypeError,ValueError) as exc:
        raise CompanyModelSpecError('signed coefficient proof differs from exact parent response') from exc


def assert_only_coefficient_changed(original: Any, repaired: Any, proof) -> None:
    validate_coefficient_parent(original,proof)
    path=proof['coefficient_path']
    restored=copy.deepcopy(repaired)
    try:
        left=original;right=restored
        for key in path[:-1]:left=left[key];right=right[key]
        if (left[path[-1]] != proof['old_coefficient']
                or right[path[-1]] != proof['required_coefficient']):raise ValueError()
        right[path[-1]]=left[path[-1]]
        if canonical_json(restored)!=canonical_json(original):raise ValueError()
    except (KeyError,IndexError,TypeError,ValueError) as exc:
        raise CompanyModelSpecError('signed expense repair changed fields outside its exact coefficient scope', code=ERROR_CODE) from exc


def validate_financials_for_repair(missions, spec, *, note_evidence_resolver=None):
    """Read/validate only; the normal final persistence boundary runs again."""
    try:
        return missions.validate_company_model_spec_financials(spec,
            note_evidence_resolver=note_evidence_resolver)
    except FinancialStatementStructureError as exc:
        if not _recognized_tie_failure(spec,str(exc)):raise
        from .company_model_inputs import build_model_inputs
        from .coverage_mission import company_model_spec_id
        candidate={**dict(spec),'spec_id':company_model_spec_id(spec['company_ref'],spec['state_hash'],spec['task_hash'])}
        inputs=build_model_inputs(missions,candidate)
        try:
            proof=repair_proof(spec,inputs,str(exc),note_evidence_resolver=note_evidence_resolver)
        except (FinancialStatementStructureError,ValueError,KeyError,TypeError,ArithmeticError):
            raise exc
        if proof is None:raise
        raise SignedExpenseRepairError(proof) from exc
