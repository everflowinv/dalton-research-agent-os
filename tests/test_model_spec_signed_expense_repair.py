"""Source-proven sign mistakes use one bounded repair, never changed facts."""
import copy,json,unittest
from unittest.mock import patch
from dalton_core import company_model_cli as cli, company_model_spec as specs
from dalton_core.company_financial_statement_structure import (
    FinancialStatementStructureError, materialize_financial_statement_structure)
from dalton_core.model_spec_signed_expense_repair import (
    repair_proof, SignedExpenseRepairError, assert_only_coefficient_changed, validate_coefficient_parent, validate_financials_for_repair)
SIGNED_CONCEPT="us-gaap:OtherOperatingIncomeExpenseNet"
from tests import test_model_spec_eps_numerator_bridge as bridge
from tests import test_company_financial_statement_structure as financial
from tests import test_model_spec_cash_flow_base_repair as calls
from dalton_core.coverage_mission import company_model_spec_id


def case():
    state,body=bridge.model_reply();accession=state['filings'][0]['accession']
    state['concepts'].append(SIGNED_CONCEPT)
    state['statements']['income'].append({'concept':SIGNED_CONCEPT,'label':'Signed other operating item',
        'unit':'USD','period_kind':'duration','level':0,'parent_concept':None,
        'is_breakdown':False,'dimension_axis':None})
    line=financial.filed('signed-other','other_operating_income_expense',SIGNED_CONCEPT,forecast='unavailable')
    body['financial_statement_structure']['lines'].append(line)
    body['financial_statement_structure']['formulas'][0]['terms'].append({'line_ref':'signed-other','coefficient':'-1'})
    values={'us-gaap:Revenues':1000,'us-gaap:CostOfRevenue':600,
        'us-gaap:SellingGeneralAndAdministrativeExpense':200,'us-gaap:OperatingIncomeLoss':190,
        'us-gaap:IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest':190,
        'us-gaap:IncomeTaxExpenseBenefit':40,'us-gaap:NetIncomeLoss':150,
        'synthetic:SharesDiluted':100,'synthetic:DilutedEPS':'1.5',SIGNED_CONCEPT:-10}
    filed=[]
    for concept,value in values.items():
        unit='shares' if concept=='synthetic:SharesDiluted' else 'usd_per_share' if concept=='synthetic:DilutedEPS' else 'usd'
        row=financial.input_line(concept,[value]*4,unit=unit)
        for cell in row['cells'].values():cell['source_accessions']=[accession]
        filed.append(row)
    inputs={'schema_version':'0.2','company_ref':state['company_ref'],'periods':[p[1]for p in financial.QUARTERS],
        'filed_lines':filed,'rows':[],'cash_flow_inputs':[],
        'spec_ref':company_model_spec_id(state['company_ref'],state['state_hash'],specs.TASK_HASH)}
    return state,body,inputs


def fixed(body):
    b=copy.deepcopy(body);b['financial_statement_structure']['formulas'][0]['terms'][-1]['coefficient']='1';return b


def callback(inputs, count):
    def validate(spec):
        count.append(spec['content_hash'])
        candidate={**spec,'spec_id':company_model_spec_id(spec['company_ref'],spec['state_hash'],spec['task_hash'])}
        numeric={**copy.deepcopy(inputs),'spec_ref':candidate['spec_id']}
        try:return materialize_financial_statement_structure(candidate,numeric)
        except FinancialStatementStructureError as exc:
            proof=repair_proof(spec,numeric,str(exc))
            if proof is None:raise
            raise SignedExpenseRepairError(proof) from exc
    return validate


class SignedExpenseRepairTests(unittest.TestCase):
    def run_loop(self,state,bad,answer,*,max_attempts=1,prior=None):
        model=type('Model',(),{'scheduler_db':None})()
        model.calls=[]
        def call(**kw):model.calls.append(kw);return {**calls.root_call(answer),'cost_micros':47,'work_order_ref':'work:repair'}
        model.call=call;attempts=[];validations=[]
        with patch.object(cli,'_prior_contract_repair',return_value=prior):
            result=cli._validated_spec_with_repair(model=model,state=state,mission={},
                original_call=calls.root_call(bad) if not isinstance(bad,str) else {**calls.root_call({}),'text':bad},
                repair_config={'max_attempts':max_attempts},decided_by='automation:test',
                repair_attempts=attempts,financial_validator=callback(case()[2],validations))
        return result,model,attempts,validations

    def test_real_parser_one_repair_revalidates_all_periods_and_preserves_paid_refs(self):
        state,bad,inputs=case();before=copy.deepcopy(inputs)
        result,model,attempts,validations=self.run_loop(state,bad,fixed(bad))
        self.assertEqual(len(model.calls),1);self.assertEqual(len(validations),2)
        self.assertEqual(inputs,before);self.assertEqual(attempts[0]['cost_micros'],47)
        proof=json.loads(model.calls[0]['_structured_output_repair']['validation_error']['message'])
        self.assertEqual(len(proof['periods']),4)
        self.assertEqual(proof['periods'][0]['actual'],'210');self.assertEqual(proof['periods'][0]['expected'],'190')
        self.assertIn('Do not change any other field',model.calls[0]['prompt'])
        self.assertEqual(result[2]['work_order_ref'],'work:repair')

    def test_foreign_concept_no_period_partial_tie_and_multiple_candidates_do_not_open(self):
        for change in ('unknown','empty','partial','multiple','positive'):
            state,bad,inputs=case();spec=specs.spec_from_response(state,json.dumps(bad),decided_by='automation:test')
            signed=next(r for r in inputs['filed_lines'] if r['concept']==SIGNED_CONCEPT)
            if change=='unknown':signed['status']='not_found'
            if change=='empty':signed['cells']={}
            if change=='partial':next(r for r in inputs['filed_lines'] if r['concept']=='us-gaap:OperatingIncomeLoss')['cells']['2025-12-31']['value']='188'
            if change=='multiple':
                other=copy.deepcopy(spec['financial_statement_structure']['lines'][-1]);other['ref']='another'
                spec['financial_statement_structure']['lines'].append(other)
                spec['financial_statement_structure']['formulas'][0]['terms'].append({'line_ref':'another','coefficient':'-1'})
            if change=='positive':
                for cell in signed['cells'].values():cell['value']='10'
            with self.subTest(change=change):
                self.assertIsNone(repair_proof(spec,inputs,'formula operating does not tie to filed history'))

    def test_other_company_concept_positive_and_mixed_signs_use_full_graph_proof(self):
        for kind in ('other_company','positive','mixed'):
            state,body,inputs=case()
            line=body['financial_statement_structure']['lines'][-1]
            signed=next(r for r in inputs['filed_lines'] if r['concept']==SIGNED_CONCEPT)
            if kind=='other_company':
                state['company_ref']=inputs['company_ref']='company:synthetic:another-issuer'
                state['concepts'][-1]='synthetic:ReportedOtherExpense'
                state['statements']['income'][-1]['concept']='synthetic:ReportedOtherExpense'
                line.update(concept='synthetic:ReportedOtherExpense',role='operating_expense')
                signed['concept']='synthetic:ReportedOtherExpense'
            if kind=='positive':
                line['role']='operating_expense'
                body['financial_statement_structure']['formulas'][0]['terms'][-1]['coefficient']='1'
                for cell in signed['cells'].values():cell['value']='10'
            if kind=='mixed':
                operating=next(r for r in inputs['filed_lines'] if r['concept']=='us-gaap:OperatingIncomeLoss')
                for i,(period,cell) in enumerate(signed['cells'].items()):
                    cell['value']='-10' if i%2==0 else '10'
                    operating['cells'][period]['value']='190' if i%2==0 else '210'
            spec=specs.spec_from_response(state,json.dumps(body),decided_by='automation:test')
            inputs['spec_ref']=company_model_spec_id(spec['company_ref'],spec['state_hash'],spec['task_hash'])
            with self.subTest(kind=kind):
                proof=repair_proof(spec,inputs,'formula operating does not tie to filed history')
                self.assertIsNotNone(proof)
                self.assertEqual(proof['required_coefficient'],'-1' if kind=='positive' else '1')
                self.assertEqual(len(proof['periods']),4)

    def test_positive_interest_income_in_pretax_sum_is_generic_not_operating_specific(self):
        inputs=financial.financial_inputs();candidate=financial.proposal(inputs)
        definition={k:candidate[k]for k in ('schema_version','lines','formulas')}
        next(t for f in definition['formulas'] if f['output_ref']=='pretax'
             for t in f['terms'] if t['line_ref']=='interest-income')['coefficient']='-1'
        spec={**financial.company_spec(),'financial_statement_structure':definition,'decided_by':'automation:test'}
        proof=repair_proof(spec,inputs,'formula pretax does not tie to filed history')
        self.assertIsNotNone(proof)
        self.assertEqual((proof['line_ref'],proof['concept'],proof['required_coefficient']),
                         ('interest-income','interest_income','1'))

    def test_two_individually_valid_corrections_are_ambiguous_and_not_authorized(self):
        state,body,inputs=case();spec=specs.spec_from_response(state,json.dumps(body),decided_by='automation:test')
        line=copy.deepcopy(spec['financial_statement_structure']['lines'][-1])
        line.update(ref='second-expense',concept='synthetic:SecondExpense',role='operating_expense')
        spec['financial_statement_structure']['lines'].append(line)
        spec['financial_statement_structure']['formulas'][0]['terms'].append({'line_ref':'second-expense','coefficient':'-1'})
        source=copy.deepcopy(next(r for r in inputs['filed_lines'] if r['concept']==SIGNED_CONCEPT))
        source['concept']='synthetic:SecondExpense';inputs['filed_lines'].append(source)
        for cell in next(r for r in inputs['filed_lines'] if r['concept']=='us-gaap:OperatingIncomeLoss')['cells'].values():cell['value']='200'
        self.assertIsNone(repair_proof(spec,inputs,'formula operating does not tie to filed history'))

    def test_downstream_mismatch_and_invalid_source_unit_cannot_open_a_repair(self):
        for kind in ('downstream','unit','zero'):
            state,body,inputs=case();spec=specs.spec_from_response(state,json.dumps(body),decided_by='automation:test')
            if kind=='downstream':
                for cell in next(r for r in inputs['filed_lines'] if r['concept']=='synthetic:DilutedEPS')['cells'].values():cell['value']='9.9'
            if kind=='unit':
                for cell in next(r for r in inputs['filed_lines'] if r['concept']==SIGNED_CONCEPT)['cells'].values():cell['unit']='shares'
            if kind=='zero':
                for cell in next(r for r in inputs['filed_lines'] if r['concept']==SIGNED_CONCEPT)['cells'].values():cell['value']='0'
            with self.subTest(kind=kind):self.assertIsNone(repair_proof(spec,inputs,'formula operating does not tie to filed history'))

    def test_unrecognized_failure_preserves_original_exception_without_rebuilding_inputs(self):
        class Missions:
            def validate_company_model_spec_financials(self,*args,**kwargs):
                raise FinancialStatementStructureError('unknown original unit/source error')
        with patch('dalton_core.company_model_inputs.build_model_inputs',side_effect=AssertionError('must not rebuild')):
            with self.assertRaisesRegex(FinancialStatementStructureError,'unknown original unit/source error'):
                validate_financials_for_repair(Missions(),{'financial_statement_structure':{'formulas':[],'lines':[]}})

    def test_parent_order_or_numeric_normalization_cannot_authorize_wrong_path(self):
        state,body,inputs=case();spec=specs.spec_from_response(state,json.dumps(body),decided_by='automation:test')
        proof=repair_proof(spec,inputs,'formula operating does not tie to filed history')
        for kind in ('order','number'):
            changed=copy.deepcopy(body)
            terms=changed['financial_statement_structure']['formulas'][0]['terms']
            if kind=='order':terms[0],terms[-1]=terms[-1],terms[0]
            else:terms[-1]['coefficient']=-1
            with self.subTest(kind=kind),self.assertRaisesRegex(specs.CompanyModelSpecError,'exact parent response'):
                validate_coefficient_parent(changed,proof)

    def test_disabled_without_historical_answer_never_calls_model(self):
        state,bad,_=case()
        with self.assertRaises(SignedExpenseRepairError):self.run_loop(state,bad,fixed(bad),max_attempts=0)

    def test_disabled_can_freely_replay_exact_correct_historical_answer_and_revalidate(self):
        state,bad,_=case();prior={**calls.root_call(fixed(bad)),'repair_number':1,
            'binding_hash':'a'*64,'repair_contract_hash':'b'*64,'work_order_ref':'work:old-repair'}
        result,model,attempts,validations=self.run_loop(state,bad,fixed(bad),max_attempts=0,prior=prior)
        self.assertEqual(model.calls,[]);self.assertEqual(len(validations),2)
        self.assertEqual(attempts[0]['cost_micros'],0);self.assertEqual(result[2]['work_order_ref'],'work:old-repair')

    def test_an_already_used_repair_cannot_trigger_another_signed_repair(self):
        state,bad,_=case();count=[];attempts=[]
        class Model:
            scheduler_db=None
            def call(self,**kw):count.append(kw);return {**calls.root_call(bad),'cost_micros':47}
        with patch.object(cli,'_prior_contract_repair',return_value=None),self.assertRaises(SignedExpenseRepairError):
            cli._validated_spec_with_repair(model=Model(),state=state,mission={},
                original_call={**calls.root_call({}),'text':'{'},repair_config={'max_attempts':5},
                decided_by='automation:test',repair_attempts=attempts,financial_validator=callback(case()[2],[]))
        self.assertEqual(len(count),1);self.assertEqual(attempts[0]['cost_micros'],47)

    def test_paid_answer_cannot_change_other_fields_and_cost_is_retained(self):
        state,bad,inputs=case();spec=specs.spec_from_response(state,json.dumps(bad),decided_by='automation:test')
        proof=repair_proof(spec,inputs,'formula operating does not tie to filed history')
        for change in ('assessment','evidence','concept','coefficient'):
            answer=fixed(bad)
            if change=='assessment':answer['assessment']='Different economic thesis.'
            if change=='evidence':answer['financial_statement_structure']['formulas'][0]['evidence_refs']=['other']
            if change=='concept':answer['financial_statement_structure']['lines'][-1]['concept']='invented:Other'
            if change=='coefficient':answer['financial_statement_structure']['formulas'][0]['terms'][-1]['coefficient']='-1'
            with self.subTest(change=change),self.assertRaises(specs.CompanyModelSpecError):
                assert_only_coefficient_changed(bad,answer,proof)

    def test_paid_scope_violation_retains_accounting_and_stops_at_one_call(self):
        state,bad,inputs=case();answer=fixed(bad);answer['assessment']='Reinterpreted business'
        sent=[];attempts=[]
        class Model:
            scheduler_db=None
            def call(self,**kwargs):
                sent.append(kwargs)
                return {**calls.root_call(answer),'cost_micros':47,'invocation_ref':'invocation:paid-repair'}
        with patch.object(cli,'_prior_contract_repair',return_value=None),self.assertRaisesRegex(specs.CompanyModelSpecError,'exact coefficient scope'):
            cli._validated_spec_with_repair(model=Model(),state=state,mission={},original_call=calls.root_call(bad),
                repair_config={'max_attempts':1},decided_by='automation:test',repair_attempts=attempts,
                financial_validator=callback(inputs,[]))
        self.assertEqual(len(sent),1)
        self.assertEqual((attempts[0]['cost_micros'],attempts[0]['invocation_ref']),(47,'invocation:paid-repair'))

    def test_replayed_wrong_answer_is_scoped_and_never_sent_again(self):
        state,bad,_=case();answer=fixed(bad);answer['assessment']='Changed thesis'
        prior={**calls.root_call(answer),'repair_number':1,'binding_hash':'a'*64,'repair_contract_hash':'b'*64}
        with self.assertRaisesRegex(specs.CompanyModelSpecError,'exact coefficient scope'):
            self.run_loop(state,bad,answer,max_attempts=0,prior=prior)
