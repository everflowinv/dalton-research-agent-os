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
from .cash_flow_ratio_applicability import CashRatioUnavailable
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


# Kept separate from 0.1: existing single-coefficient proofs retain their exact
# shape and content-addressed historical replay identity.
JOINT_CONTRACT_REF = 'model-spec-signed-filed-income-sum-coefficients:0.2'
MAX_JOINT_CHANGES = 4
MAX_ELIGIBLE_PATHS = 20


def _joint_values(definition, filed, changes=()):
    lines = {r['ref']: r for r in definition['lines']}
    values = {r['ref']: _period_cells(filed[r['concept']]) for r in lines.values()
              if r.get('kind') == 'filed' and r.get('concept') in filed}
    overrides = {(c['coefficient_path'][2], c['coefficient_path'][4]):
                 c['required_coefficient'] for c in changes}
    pending = list(enumerate(definition['formulas']))
    while pending:
        progress = False
        for fi, formula in list(pending):
            dependencies = ([t['line_ref'] for t in formula['terms']]
                            if formula['operator'] == 'sum' else
                            [formula['numerator_ref'], formula['denominator_ref']])
            if any(ref not in values for ref in dependencies):
                continue
            periods = set(values[dependencies[0]])
            for ref in dependencies[1:]:
                periods &= set(values[ref])
            calculated = {}
            for period in periods:
                if formula['operator'] == 'sum':
                    calculated[period] = sum((Decimal(overrides.get((fi, ti), t['coefficient']))
                        * values[t['line_ref']][period] for ti, t in enumerate(formula['terms'])), Decimal(0))
                elif values[formula['denominator_ref']][period] != 0:
                    calculated[period] = (values[formula['numerator_ref']][period]
                                          / values[formula['denominator_ref']][period])
            values[formula['output_ref']] = calculated
            pending.remove((fi, formula))
            progress = True
        if not progress:
            return None
    return values


def joint_repair_proof(spec, inputs, failure_message, *, note_evidence_resolver=None):
    """Prove one unique 2..4-path correction, without changing any source fact.

    Every originally complete comparable window must be present; unmatched
    reported windows remain explicit gaps, never declared proven. Every proposal is subsequently checked by the unchanged
    full financial and cash-flow authority validator.
    """
    from itertools import combinations
    if not _recognized_tie_failure(spec, failure_message):
        return None
    definition = spec['financial_statement_structure']
    lines = {r['ref']: r for r in definition['lines']}
    filed = {r['concept']: r for r in inputs['filed_lines']}
    original_values = _joint_values(definition, filed)
    if original_values is None:
        return None
    paths = []
    sum_targets = []
    for fi, formula in enumerate(definition['formulas']):
        output = lines[formula['output_ref']]
        if formula['operator'] != 'sum':
            continue
        target = filed.get(formula.get('tie_out_concept'), {})
        if target:
            tied = _period_cells(target)
            if (target.get('status') != 'filed' or target.get('period_basis') != 'duration'
                    or not tied or not set(tied) & set(original_values[formula['output_ref']])):
                return None
            comparable = set(tied) & set(original_values[formula['output_ref']])
            sum_targets.append((formula, {p: tied[p] for p in comparable}, tied))
        if output.get('statement') != 'income' or output.get('role') not in OUTPUT_ROLES or not target:
            continue
        for ti, term in enumerate(formula['terms']):
            line = lines[term['line_ref']]
            source = filed.get(line.get('concept'), {})
            cells = source.get('cells', {})
            if (line.get('kind') != 'filed' or line.get('statement') != 'income'
                    or line.get('role') not in TERM_ROLES or term['coefficient'] not in {'-1', '1'}
                    or source.get('status') != 'filed' or source.get('statement') != 'income'
                    or source.get('period_basis') != 'duration' or not cells
                    or any(not c.get('source_accessions') for c in cells.values())
                    or not (set(_period_cells(target)) & set(original_values[formula['output_ref']])) <= set(_period_cells(source))
                    or not any(_period_cells(source)[p] != 0 for p in
                        set(_period_cells(target)) & set(original_values[formula['output_ref']]))):
                continue
            paths.append({'output_ref': formula['output_ref'], 'tie_out_concept': formula['tie_out_concept'],
                'coefficient_path': ['financial_statement_structure','formulas',fi,'terms',ti,'coefficient'],
                'line_ref': line['ref'], 'concept': line['concept'],
                'old_coefficient': term['coefficient'],
                'required_coefficient': str(-int(term['coefficient'])), 'evidence_refs': formula['evidence_refs']})
    # Bound pure enumeration. A larger/unknown model remains a refusal, never
    # a partial search mislabeled unique.
    if not 2 <= len(paths) <= MAX_ELIGIBLE_PATHS:
        return None
    candidates = []
    examined = 0
    full_validations = 0
    for size in range(1, min(MAX_JOINT_CHANGES, len(paths)) + 1):
        for combo in combinations(paths, size):
            examined += 1
            values = _joint_values(definition, filed, combo)
            if values is None or any(any(values[f['output_ref']].get(p) != expected
                    for p, expected in tied.items()) for f, tied, reported in sum_targets):
                continue
            candidate = copy.deepcopy(spec)
            for change in combo:
                path = change['coefficient_path']
                candidate['financial_statement_structure']['formulas'][path[2]]['terms'][path[4]]['coefficient'] = change['required_coefficient']
            candidate.pop('content_hash', None)
            spec_id = candidate.pop('spec_id', None)
            candidate['content_hash'] = content_hash(candidate)
            candidate['spec_id'] = spec_id or inputs.get('spec_ref')
            full_validations += 1
            try:
                from .company_financial_statement_structure import materialize_financial_statement_structure
                from .model_forecast_driver import build_cash_flow_companion, ForecastModelError
                structure, replay = materialize_financial_statement_structure(candidate, inputs,
                    note_evidence_resolver=note_evidence_resolver)
                if any(r['status'] != 'validated' or not r['tested_periods'] for r in replay['formulas']):
                    continue
                if candidate.get('schema_version') == '0.4':
                    build_cash_flow_companion(candidate, inputs, structure)
            except (FinancialStatementStructureError, ForecastModelError, ValueError, KeyError, ArithmeticError):
                continue
            reports = []
            for formula, tied, reported in sum_targets:
                reports.append({'output_ref': formula['output_ref'], 'tie_out_concept': formula['tie_out_concept'],
                    'reported_period_count': len(reported), 'comparable_period_count': len(tied),
                    'uncovered_periods': [{'period_start': p[0], 'period_end': p[1]}
                        for p in sorted(set(reported)-set(tied), key=lambda p: (p[1], p[0] or ''))],
                    'periods': [{'period_start': p[0], 'period_end': p[1],
                        'actual': str(original_values[formula['output_ref']][p]),
                        'expected': str(tied[p]), 'corrected': str(values[formula['output_ref']][p])}
                        for p in sorted(tied, key=lambda p: (p[1], p[0] or ''))]})
            candidates.append({'contract_ref': JOINT_CONTRACT_REF, 'spec_hash': spec.get('content_hash'),
                'financial_inputs_hash': content_hash(inputs), 'changes': list(combo),
                'formula_periods': reports, 'full_financial_replay_hash': content_hash(replay)})
            if len(candidates) > 1:
                return None
    if len(candidates) != 1 or len(candidates[0]['changes']) < 2:
        return None
    proof = candidates[0]
    proof['search'] = {'eligible_path_count': len(paths), 'examined_combination_count': examined,
        'unique_valid_candidate_count': 1, 'max_changed_coefficients': MAX_JOINT_CHANGES,
        'max_eligible_paths': MAX_ELIGIBLE_PATHS, 'max_combination_count': 6195,
        'single_path_alternatives_also_exhausted': True,
        'full_validation_count': full_validations,
        'coverage': 'all_original_complete_comparable_periods_no_new_missing_period_tolerance'}
    proof['content_hash'] = content_hash(proof)
    return proof


def _proof_changes(proof):
    if proof.get('contract_ref') == JOINT_CONTRACT_REF:
        changes = proof.get('changes')
        if (not isinstance(changes, list) or not 2 <= len(changes) <= MAX_JOINT_CHANGES
                or proof.get('content_hash') != content_hash({k:v for k,v in proof.items() if k != 'content_hash'})
                or len({tuple(c['coefficient_path']) for c in changes}) != len(changes)):
            raise CompanyModelSpecError('joint signed coefficient proof is invalid')
        return changes
    return [proof]


def _validate_single_coefficient_parent(original, proof):
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


def validate_coefficient_parent(original, proof):
    for change in _proof_changes(proof):
        _validate_single_coefficient_parent(original, change)


def assert_only_coefficient_changed(original: Any, repaired: Any, proof) -> None:
    validate_coefficient_parent(original,proof)
    restored=copy.deepcopy(repaired)
    try:
        for change in _proof_changes(proof):
            path=change['coefficient_path']
            left=original;right=restored
            for key in path[:-1]:left=left[key];right=right[key]
            if (left[path[-1]] != change['old_coefficient']
                    or right[path[-1]] != change['required_coefficient']):raise ValueError()
            right[path[-1]]=left[path[-1]]
        if canonical_json(restored)!=canonical_json(original):raise ValueError()
    except (KeyError,IndexError,TypeError,ValueError) as exc:
        raise CompanyModelSpecError('signed expense repair changed fields outside its exact coefficient scope', code=ERROR_CODE) from exc


def validate_financials_for_repair(missions, spec, *, note_evidence_resolver=None):
    """Read/validate only; the normal final persistence boundary runs again."""
    try:
        return missions.validate_company_model_spec_financials(spec,
            note_evidence_resolver=note_evidence_resolver)
    except CashRatioUnavailable as exc:
        from .cash_flow_ratio_applicability import repair_error
        from .company_model_inputs import build_model_inputs
        from .coverage_mission import company_model_spec_id
        candidate={**dict(spec),'spec_id':company_model_spec_id(spec['company_ref'],spec['state_hash'],spec['task_hash'])}
        inputs=build_model_inputs(missions,candidate)
        error=repair_error(candidate,inputs,exc.diagnostic,note_evidence_resolver=note_evidence_resolver)
        if error is None:raise
        raise error from exc
    except FinancialStatementStructureError as exc:
        if not _recognized_tie_failure(spec,str(exc)):raise
        from .company_model_inputs import build_model_inputs
        from .coverage_mission import company_model_spec_id
        candidate={**dict(spec),'spec_id':company_model_spec_id(spec['company_ref'],spec['state_hash'],spec['task_hash'])}
        inputs=build_model_inputs(missions,candidate)
        try:
            proof=repair_proof(spec,inputs,str(exc),note_evidence_resolver=note_evidence_resolver)
            if proof is None:
                proof=joint_repair_proof(spec,inputs,str(exc),note_evidence_resolver=note_evidence_resolver)
        except (FinancialStatementStructureError,ValueError,KeyError,TypeError,ArithmeticError):
            raise exc
        if proof is None:raise
        raise SignedExpenseRepairError(proof) from exc
