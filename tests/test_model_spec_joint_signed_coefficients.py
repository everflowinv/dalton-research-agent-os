"""Bounded joint coefficient proofs preserve source facts and one repair limit."""
import copy
import json
import unittest
from unittest.mock import patch
from dalton_core import company_model_cli as cli, company_model_spec as specs
from dalton_core.company_financial_statement_structure import (
    FinancialStatementStructureError, materialize_financial_statement_structure)
from dalton_core.coverage_mission import company_model_spec_id
from dalton_core.model_spec_signed_expense_repair import (
    JOINT_CONTRACT_REF, SignedExpenseRepairError, joint_repair_proof, repair_proof,
    assert_only_coefficient_changed, validate_coefficient_parent)
from tests import test_model_spec_signed_expense_repair as single
from tests import test_company_financial_statement_structure as financial
from tests import test_model_spec_cash_flow_base_repair as calls


def case(count=3):
    state, body, inputs = single.case()
    # Different reported concepts, companies and roles use the same mechanism.
    state['company_ref'] = inputs['company_ref'] = 'company:synthetic:joint-issuer'
    for i in range(1, count):
        concept = f'synthetic:ReportedExpense{i}'
        state['concepts'].append(concept)
        state['statements']['income'].append({**state['statements']['income'][-1], 'concept': concept})
        body['financial_statement_structure']['lines'].append(
            financial.filed(f'expense-{i}', 'operating_expense', concept, forecast='unavailable'))
        body['financial_statement_structure']['formulas'][0]['terms'].append(
            {'line_ref': f'expense-{i}', 'coefficient': '-1'})
        inputs['filed_lines'].append(financial.input_line(concept, [-10 * (i+1)] * 4))
    operating = 200 - 10 * count * (count+1) // 2
    for concept, value in {
        'us-gaap:OperatingIncomeLoss': operating,
        'us-gaap:IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest': operating,
        'us-gaap:NetIncomeLoss': operating-40,
        'synthetic:DilutedEPS': str((operating-40)/100),
    }.items():
        for c in next(r for r in inputs['filed_lines'] if r['concept'] == concept)['cells'].values():
            c['value'] = str(value)
    return state, body, inputs


def parsed(state, body, inputs):
    spec = specs.spec_from_response(state, json.dumps(body), decided_by='automation:test')
    inputs['spec_ref'] = company_model_spec_id(spec['company_ref'], spec['state_hash'], spec['task_hash'])
    return spec


def callback(inputs):
    def validate(spec):
        candidate = {**spec, 'spec_id': company_model_spec_id(spec['company_ref'], spec['state_hash'], spec['task_hash'])}
        numeric = {**copy.deepcopy(inputs), 'spec_ref': candidate['spec_id']}
        try:
            return materialize_financial_statement_structure(candidate, numeric)
        except FinancialStatementStructureError as error:
            proof = repair_proof(spec, numeric, str(error)) or joint_repair_proof(spec, numeric, str(error))
            if proof is None:
                raise
            raise SignedExpenseRepairError(proof) from error
    return validate


class JointSignedCoefficientTests(unittest.TestCase):
    def test_two_three_four_paths_unique_complete_graph_and_sources_unchanged(self):
        for count in (2, 3, 4):
            state, body, inputs = case(count)
            spec = parsed(state, body, inputs)
            before = copy.deepcopy((spec, inputs))
            proof = joint_repair_proof(spec, inputs, 'formula operating does not tie to filed history')
            with self.subTest(count=count):
                self.assertEqual(proof['contract_ref'], JOINT_CONTRACT_REF)
                self.assertEqual(len(proof['changes']), count)
                self.assertEqual(proof['search']['unique_valid_candidate_count'], 1)
                self.assertTrue(all(r['comparable_period_count'] == 4 and not r['uncovered_periods']
                                    for r in proof['formula_periods']))
                answer = self.correct(body, proof)
                assert_only_coefficient_changed(body, answer, proof)
                self.assertEqual((spec, inputs), before)
                callback(inputs)(parsed(state, answer, inputs))

    @staticmethod
    def correct(body, proof):
        answer = copy.deepcopy(body)
        for change in proof['changes']:
            path = change['coefficient_path']
            answer[path[0]][path[1]][path[2]][path[3]][path[4]][path[5]] = change['required_coefficient']
        return answer

    def test_positive_and_mixed_signed_facts_and_distinct_formula_roles(self):
        for kind in ('positive','mixed','pretax'):
            state,body,inputs=case(2)
            if kind=='pretax':
                inputs=financial.financial_inputs();definition=financial.proposal(inputs)
                for formula in definition['formulas']:
                    for term in formula.get('terms',[]):
                        if term['line_ref'] in ('cost','interest-income'):
                            term['coefficient']=str(-int(term['coefficient']))
                spec={**financial.company_spec(),'financial_statement_structure':{
                    k:definition[k]for k in ('schema_version','lines','formulas')},'decided_by':'automation:test'}
                proof=joint_repair_proof(spec,inputs,'formula operating does not tie to filed history')
                self.assertIsNotNone(proof)
                self.assertEqual({c['line_ref']for c in proof['changes']},{'cost','interest-income'})
                continue
            signed=next(r for r in inputs['filed_lines']if r['concept']==single.SIGNED_CONCEPT)
            targets={r['concept']:r for r in inputs['filed_lines']}
            if kind=='positive':
                body['financial_statement_structure']['formulas'][0]['terms'][-2]['coefficient']='1'
            for i,(period,cell) in enumerate(signed['cells'].items()):
                cell['value']='10'if kind=='positive' or i%2 else'-10'
                op=200 + (int(cell['value'])if kind=='mixed'else-int(cell['value'])) -20
                for concept,value in {'us-gaap:OperatingIncomeLoss':op,
                    'us-gaap:IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest':op,
                    'us-gaap:NetIncomeLoss':op-40,'synthetic:DilutedEPS':str((op-40)/100)}.items():
                    targets[concept]['cells'][period]['value']=str(value)
            with self.subTest(kind=kind):
                proof=joint_repair_proof(parsed(state,body,inputs),inputs,'formula operating does not tie to filed history')
                self.assertIsNotNone(proof);self.assertEqual(len(proof['changes']),2)

    def test_five_changes_downstream_error_missing_source_unit_zero_unknown_are_refused(self):
        for kind in ('five', 'downstream', 'source', 'unit', 'zero', 'unknown', 'period'):
            state, body, inputs = case(5 if kind == 'five' else 3)
            source = next(r for r in inputs['filed_lines'] if r['concept'] == single.SIGNED_CONCEPT)
            if kind == 'downstream':
                next(r for r in inputs['filed_lines'] if r['concept'] == 'synthetic:DilutedEPS')['cells']['2025-12-31']['value'] = '9'
            if kind == 'source':
                source['cells']['2025-12-31']['source_accessions'] = []
            if kind == 'unit':
                source['cells']['2025-12-31']['unit'] = 'shares'
            if kind == 'zero':
                for c in source['cells'].values(): c['value'] = '0'
            if kind == 'unknown': source['status'] = 'not_found'
            if kind == 'period':
                # One supplied complete historical period disagrees: it must not
                # be removed from the fixed comparable set to make proof pass.
                next(r for r in inputs['filed_lines'] if r['concept'] == 'us-gaap:OperatingIncomeLoss')['cells']['2025-12-31']['value'] = '141'
            with self.subTest(kind=kind):
                self.assertIsNone(joint_repair_proof(parsed(state, body, inputs), inputs,
                    'formula operating does not tie to filed history'))

    def test_ambiguous_joint_candidates_are_refused(self):
        state, body, inputs = case(3)
        # Either second or duplicate second expense can be flipped; the
        # additional original expense cancels one of the pre-existing terms.
        concept = 'synthetic:DuplicateExpense'
        state['concepts'].append(concept)
        state['statements']['income'].append({**state['statements']['income'][-1], 'concept': concept})
        body['financial_statement_structure']['lines'].append(financial.filed('duplicate', 'operating_expense', concept))
        body['financial_statement_structure']['formulas'][0]['terms'].append({'line_ref':'duplicate','coefficient':'-1'})
        inputs['filed_lines'].append(financial.input_line(concept, [-20]*4))
        for concept, value in {'us-gaap:OperatingIncomeLoss':180,
            'us-gaap:IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest':180,
            'us-gaap:NetIncomeLoss':140, 'synthetic:DilutedEPS':'1.4'}.items():
            for c in next(r for r in inputs['filed_lines'] if r['concept']==concept)['cells'].values():c['value']=str(value)
        self.assertIsNone(joint_repair_proof(parsed(state,body,inputs),inputs,
            'formula operating does not tie to filed history'))

    def test_exact_multi_path_parent_and_repaired_body_boundaries(self):
        state, body, inputs = case()
        proof = joint_repair_proof(parsed(state,body,inputs),inputs,'formula operating does not tie to filed history')
        for kind in ('fact', 'formula', 'assessment', 'evidence', 'missing_change', 'order'):
            answer = self.correct(body,proof)
            if kind=='fact': answer['financial_statement_structure']['lines'][-1]['concept']='invented:Expense'
            if kind=='formula': answer['financial_statement_structure']['formulas'][0]['terms'][0]['coefficient']='-1'
            if kind=='assessment': answer['assessment']='different'
            if kind=='evidence': answer['financial_statement_structure']['formulas'][0]['evidence_refs']=['invented']
            if kind=='missing_change': answer['financial_statement_structure']['formulas'][0]['terms'][-1]['coefficient']='-1'
            if kind=='order': answer['financial_statement_structure']['formulas'][0]['terms'].reverse()
            with self.subTest(kind=kind), self.assertRaises(specs.CompanyModelSpecError):
                assert_only_coefficient_changed(body,answer,proof)
        changed=copy.deepcopy(body);changed['financial_statement_structure']['formulas'][0]['terms'].reverse()
        with self.assertRaises(specs.CompanyModelSpecError): validate_coefficient_parent(changed,proof)

    def test_one_call_paid_refs_retained_free_replay_and_disabled_no_call(self):
        state, body, inputs = case()
        proof = joint_repair_proof(parsed(state,body,inputs),inputs,'formula operating does not tie to filed history')
        answer = self.correct(body,proof)
        for kind in ('new','free','disabled','used','wrong'):
            attempts=[];sent=[]
            class Model:
                scheduler_db=None
                def call(self,**kwargs):
                    sent.append(kwargs)
                    reply=copy.deepcopy(answer)
                    if kind=='wrong':reply['assessment']='drift'
                    return {**calls.root_call(reply if kind!='used' else body),'cost_micros':47,
                            'invocation_ref':'invocation:paid-joint'}
            prior={**calls.root_call(answer),'repair_number':1,'binding_hash':'a'*64,
                'repair_contract_hash':'b'*64,'work_order_ref':'work:prior-paid'} if kind=='free' else None
            root=calls.root_call(body)
            if kind=='used':root={**calls.root_call({}),'text':'{'}
            with self.subTest(kind=kind),patch.object(cli,'_prior_contract_repair',return_value=prior):
                run=lambda:cli._validated_spec_with_repair(model=Model(),state=state,mission={},
                    original_call=root,repair_config={'max_attempts':0 if kind in ('free','disabled') else 1},
                    decided_by='automation:test',repair_attempts=attempts,financial_validator=callback(inputs))
                if kind in ('disabled','used','wrong'):
                    with self.assertRaises((specs.CompanyModelSpecError,FinancialStatementStructureError)):run()
                else:run()
                self.assertEqual(len(sent),0 if kind in ('free','disabled') else 1)
                if sent:self.assertEqual(attempts[0]['cost_micros'],47)
                if kind=='free':self.assertEqual((attempts[0]['cost_micros'],attempts[0]['work_order_ref']),(0,'work:prior-paid'))

    def test_search_bound_refuses_without_partial_search_or_financial_replay(self):
        state,body,inputs=case(21)
        spec=parsed(state,body,inputs)
        with patch('dalton_core.model_spec_signed_expense_repair._joint_values',wraps=__import__(
            'dalton_core.model_spec_signed_expense_repair',fromlist=['_joint_values'])._joint_values) as evaluate:
            self.assertIsNone(joint_repair_proof(spec,inputs,'formula operating does not tie to filed history'))
            self.assertEqual(evaluate.call_count,1)

    def test_free_validation_marker_releases_old_hold_without_changing_task_or_policy(self):
        from tests import test_mission_model_spec_lane as lanes
        f=lanes.ModelSpecLaneTests('runTest');f.setUp();self.addCleanup(f.doCleanups)
        f.missions.companies=[lanes.ACN];f.mission['universe']=f.mission['universe'][:1]
        f.launcher.financial_validation_contract_hash=cli.model_spec_validation_contract_hash
        current=cli.model_spec_validation_contract();old={k:v for k,v in current.items()if k!='joint_signed_filed_income_sum'}
        with patch('dalton_core.company_model_cli.model_spec_validation_contract',return_value=old):
            first=f.lane.dispatch_once();f.launcher.finish(first['ticket_ref'],summary={
                'spec_status':'refused','failure_reason':'formula operating does not tie to filed history'})
            self.assertEqual(f.lane.dispatch_once()['status'],'held')
        resumed=f.lane.dispatch_once();self.assertEqual(resumed['status'],'launched')
        self.assertNotEqual(f.launcher.started[0]['financial_validation_contract_hash'],f.launcher.started[1]['financial_validation_contract_hash'])
        self.assertEqual(len(f.launcher.started),2)
        self.assertEqual(f.launcher.repair_config,{'max_attempts':0})
        f.launcher.finish(resumed['ticket_ref'],summary={'spec_status':'refused','failure_reason':'still incomplete'})
        self.assertEqual(f.lane.dispatch_once()['status'],'held')
        self.assertEqual(len(f.launcher.started),2)

    def test_old_single_proof_shape_unchanged_and_joint_marker_wakes_only_validation(self):
        state,body,inputs=single.case();spec=parsed(state,body,inputs)
        proof=repair_proof(spec,inputs,'formula operating does not tie to filed history')
        self.assertEqual(proof['contract_ref'],'model-spec-signed-filed-income-sum-coefficient:0.1')
        self.assertNotIn('changes',proof)
        self.assertEqual(cli.model_spec_validation_contract()['joint_signed_filed_income_sum'],JOINT_CONTRACT_REF)
