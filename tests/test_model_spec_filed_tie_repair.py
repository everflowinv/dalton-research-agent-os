import copy
import json
import unittest
from dataclasses import replace
from unittest.mock import patch

from dalton_core import company_model_cli as cli, company_model_spec as specs
from dalton_core import model_spec_filed_tie_repair as rule
from dalton_core.cockpit_model import CockpitModelError
from dalton_core.scheduler import Scheduler
from dalton_core.store import content_hash
from tests import test_cockpit_model_fallback as runtime
from tests import test_model_spec_signed_expense_repair as source


def case(company='company:synthetic:cedar', currency='usd'):
    state, raw, inputs=source.case();raw=source.fixed(raw)
    raw['financial_statement_structure']['schema_version']='0.6'
    for line in raw['financial_statement_structure']['lines']:
        line.setdefault('annual_forecast_method',None)
        if line['role']=='diluted_weighted_average_shares':line['annual_forecast_method']='unavailable'
    state['company_ref']=company;state['ticker']='SYNTHETIC';inputs['company_ref']=company
    if currency!='usd':
        for rows in state['statements'].values():
            for row in rows:
                if str(row.get('unit','')).casefold()=='usd':row['unit']=currency
        for line in raw['financial_statement_structure']['lines']:
            if line['unit']=='usd':line['unit']=currency
            elif line['unit']=='usd_per_share':line['unit']=currency+'_per_share'
        for line in inputs['filed_lines']:
            for cell in line['cells'].values():
                if str(cell['unit']).casefold()=='usd':cell['unit']=currency
                elif str(cell['unit']).casefold()=='usd_per_share':cell['unit']=currency+'_per_share'
    state['state_hash']=content_hash({k:v for k,v in state.items() if k!='state_hash'})
    bad=copy.deepcopy(raw);bad['financial_statement_structure']['formulas'][0]['tie_out_concept']=None
    return state,bad,raw,inputs


class FiledTieStructureTests(unittest.TestCase):
    def error(self,state,body):
        try:specs.spec_from_response(state,json.dumps(body),decided_by='automation:test')
        except specs.CompanyModelSpecError as error:return error
        self.fail('parent was not refused')

    def test_exact_structured_rule_offers_current_source_without_choosing_a_tie(self):
        for company,currency in [('company:synthetic:cedar','usd'),('company:synthetic:elm','eur')]:
            state,bad,_,_=case(company,currency);before=copy.deepcopy(bad);error=self.error(state,bad)
            self.assertEqual(error.code,rule.ERROR_CODE);rule.validate_parent(bad,error.proof,state=state)
            self.assertEqual(error.proof['diagnostic']['output_ref'],'operating')
            self.assertTrue(all(c['unit']==currency for c in error.proof['source_choices']))
            self.assertIn('us-gaap:OperatingIncomeLoss',{c['concept'] for c in error.proof['source_choices']})
            self.assertEqual(bad,before);self.assertIsNone(bad['financial_statement_structure']['formulas'][0]['tie_out_concept'])
            prompt=cli._repair_prompt(json.dumps(bad),error)
            self.assertIn('rearrange, remove or replace',prompt);self.assertNotIn('Do not add, remove or rename any line',prompt)

    def test_parser_accepted_uppercase_currency_keeps_original_bytes_and_proves_same_unit(self):
        state,bad,_,_=case()
        for line in bad['financial_statement_structure']['lines']:
            line['unit']=line['unit'].upper()
        before=copy.deepcopy(bad);error=self.error(state,bad)
        self.assertEqual(error.code,rule.ERROR_CODE)
        rule.validate_parent(bad,error.proof,state=state)
        self.assertEqual(error.proof['diagnostic']['output_line']['unit'],'usd')
        self.assertTrue(all(c['unit']=='usd' for c in error.proof['source_choices']))
        self.assertEqual(bad,before)

    def test_changed_current_input_wrong_issuer_rehashed_source_choices_are_not_parent_proof(self):
        state,bad,_,_=case();held=self.error(state,bad).proof
        for mutation in ('input','issuer','choices','parent'):
            changed=copy.deepcopy(state);proof=copy.deepcopy(held);body=copy.deepcopy(bad)
            if mutation=='input':changed['filings'][0]['accession']='0000000001-26-000001'
            if mutation=='issuer':changed['company_ref']='company:foreign'
            if mutation=='choices':proof['source_choices'][0]['concept']='invented:Subtotal';proof['source_choices_hash']=content_hash(proof['source_choices']);proof['content_hash']=content_hash({k:v for k,v in proof.items()if k!='content_hash'})
            if mutation=='parent':body['assessment']='a different economic proposal'
            if mutation in ('input','issuer'):changed['state_hash']=content_hash({k:v for k,v in changed.items() if k!='state_hash'})
            with self.subTest(mutation=mutation),self.assertRaises(ValueError):rule.validate_parent(body,proof,state=changed)

    def test_missing_or_overflow_source_catalog_and_unknown_diagnostic_never_authorize(self):
        state,bad,_,_=case();diagnostic=self.error(state,bad).proof['diagnostic']
        self.assertIsNone(rule.proof(bad,state,None))
        unknown=copy.deepcopy(diagnostic);unknown['contract_ref']='unknown:rule';unknown['content_hash']=content_hash({k:v for k,v in unknown.items() if k!='content_hash'})
        self.assertIsNone(rule.proof(bad,state,unknown))
        with patch('dalton_core.company_financial_statement_structure._presentation_filed',return_value=({},{})):
            self.assertIsNone(rule.proof(bad,state,diagnostic))
        with patch.object(rule,'MAX_CHOICES',0):
            self.assertIsNone(rule.proof(bad,state,diagnostic))

    def test_nonstructure_amount_source_unit_formula_and_assessment_changes_are_refused(self):
        state,bad,good,_=case();held=self.error(state,bad).proof
        for mutation in ('driver','expense','cash','assessment'):
            changed=copy.deepcopy(good)
            if mutation=='driver':changed['revenue_drivers'][0]['label']='different economic driver'
            if mutation=='expense':changed['expense_lines'][0]['because']='new source claim'
            if mutation=='cash':changed['cash_flow_companion']['importance']='required'
            if mutation=='assessment':changed['assessment']='invented financial amount 999'
            with self.subTest(mutation=mutation),self.assertRaises(specs.CompanyModelSpecError):rule.assert_only_structure_changed(bad,changed,held)


class ActualCockpitTieRepairTests(unittest.TestCase):
    def fixture(self,company='company:synthetic:cedar',currency='usd',reply_change=None,repair_outcome=None):
        f=runtime.CockpitChainTests('test_model_spec_repair_is_a_second_budgeted_scheduler_work');f.setUp();self.addCleanup(f.doCleanups)
        state,bad,good,inputs=case(company,currency)
        for body in (bad,good):
            for line in body['financial_statement_structure']['lines']:
                line['unit']=line['unit'].upper()
        # This is an actual graph edit: keep the same source and economics,
        # replace the unreported bridge reference by the explicitly tied one.
        for line in good['financial_statement_structure']['lines']:
            if line['ref']=='operating':line['ref']='reported-operating'
        good['financial_statement_structure']['formulas'][0]['output_ref']='reported-operating'
        if reply_change:reply_change(good)
        class Adapter(runtime.ChainAdapter):
            def __init__(self):super().__init__({});self.outputs=[bad,good if repair_outcome is None else repair_outcome]
            def execute(self,work,route,profile):
                invocation,envelope=super().execute(work,route,profile)
                output=self.outputs.pop(0)
                if isinstance(output,BaseException):raise output
                if output=='unknown':return invocation,replace(envelope,status='failed',outputs={},error={'code':'TIMEOUT','message':'provider send/fee unknown'})
                return invocation,replace(envelope,outputs={'text':json.dumps(output)})
        adapter=Adapter();model=f._model(adapter,policy_version_ref=f.pinned_policy,model_spec_output_tokens=6000)
        root=model.call(purpose='model_spec',request_id=cli.model_spec_request_id(state['state_hash'],repair_config={'max_attempts':1}),
            prompt=specs.build_prompt(state),mission=f.mission,
            _model_spec_request_identity=cli.model_spec_request_identity(state['state_hash'],repair_config={'max_attempts':1}))
        validated=[];validate=source.callback(inputs,validated)
        return f,state,bad,model,root,adapter,validate,validated

    def run_repair(self,fixture,*,max_attempts=1,attempts=None):
        f,state,_,model,root,_,validate,_=fixture
        return cli._validated_spec_with_repair(model=model,state=state,mission=f.mission,original_call=root,
            repair_config={'max_attempts':max_attempts},decided_by='automation:test',financial_validator=validate,
            repair_attempts=[] if attempts is None else attempts)

    def test_real_two_company_adapter_source_owner_full_history_and_restart_zero_calls(self):
        for company,currency in [('company:synthetic:cedar','usd'),('company:synthetic:elm','usd')]:
            fixture=self.fixture(company,currency);f,state,bad,model,root,adapter,validate,validated=fixture
            accepted,attempts,call=self.run_repair(fixture)
            self.assertEqual(len(adapter.served),2);self.assertEqual(len(attempts),1);self.assertTrue(validated)
            self.assertIn('reported-operating',{l['ref'] for l in accepted['financial_statement_structure']['lines']})
            with Scheduler(model.scheduler_db) as scheduler:
                original=copy.deepcopy(scheduler.work_order_authority(root['work_order_ref']))
                child=scheduler.work_order_authority(call['work_order_ref'])['work_order']
                self.assertEqual(child['metadata']['structured_output_repair']['filed_tie_proof']['state_hash'],state['state_hash'])
                self.assertEqual(child['budget'],original['work_order']['budget'])
            again,replayed,call2=self.run_repair(fixture,max_attempts=0)
            self.assertEqual(again,accepted);self.assertTrue(replayed[0]['replayed']);self.assertEqual(call2['work_order_ref'],call['work_order_ref'])
            self.assertEqual(len(adapter.served),2)
            with Scheduler(model.scheduler_db) as scheduler:self.assertEqual(scheduler.work_order_authority(root['work_order_ref']),original)

    def test_real_adapter_nonstructure_change_remains_paid_refused_and_never_buys_again(self):
        fixture=self.fixture(reply_change=lambda b:b.update(assessment='Changed outside the permitted structure'))
        attempts=[]
        with self.assertRaisesRegex(specs.CompanyModelSpecError,'outside'):self.run_repair(fixture,attempts=attempts)
        self.assertEqual(len(attempts),1);self.assertEqual(len(fixture[5].served),2)
        with self.assertRaisesRegex(specs.CompanyModelSpecError,'outside'):self.run_repair(fixture)
        self.assertEqual(len(fixture[5].served),2)

    def test_whole_financial_validator_still_refuses_bad_coefficients_in_allowed_structure(self):
        def wrong(body):body['financial_statement_structure']['formulas'][0]['terms'][1]['coefficient']='1'
        fixture=self.fixture(reply_change=wrong);attempts=[]
        with self.assertRaises(source.SignedExpenseRepairError) as refused:self.run_repair(fixture,attempts=attempts)
        self.assertEqual(len(refused.exception.proof['periods']),4)
        self.assertTrue(all(p['actual']=='1390' and p['expected']=='190' for p in refused.exception.proof['periods']))
        self.assertEqual(len(fixture[5].served),2);self.assertEqual(len(attempts),1)
        with self.assertRaises((specs.CompanyModelSpecError,ValueError)):self.run_repair(fixture)
        self.assertEqual(len(fixture[5].served),2)

    def test_missing_source_owner_or_rehashed_wrong_source_cannot_cross_paid_adapter(self):
        for mutation in ('owner','source','state'):
            fixture=self.fixture();f,state,bad,model,root,adapter,_,_=fixture;original=model.call
            def intercept(**kwargs):
                if '_structured_output_repair' in kwargs:
                    binding=copy.deepcopy(kwargs['_structured_output_repair'])
                    if mutation=='owner':model._model_spec_financial_repair_authority=None
                    else:
                        held=binding['filed_tie_proof']
                        if mutation=='source':held['source_choices'][0]['concept']='invented:Total';held['source_choices_hash']=content_hash(held['source_choices'])
                        else:held['state_hash']='f'*64
                        held['content_hash']=content_hash({k:v for k,v in held.items()if k!='content_hash'})
                        kwargs['_structured_output_repair']=binding;kwargs['request_id']='model-spec-repair:'+content_hash(binding)[:32]
                return original(**kwargs)
            with self.subTest(mutation=mutation),patch.object(model,'call',side_effect=intercept),self.assertRaises(CockpitModelError):self.run_repair(fixture)
            self.assertEqual(len(adapter.served),1)

    def test_disabled_original_ordinary_slot_does_not_send_a_graph_correction(self):
        fixture=self.fixture()
        with self.assertRaises(specs.CompanyModelSpecError):self.run_repair(fixture,max_attempts=0)
        self.assertEqual(len(fixture[5].served),1)

    def test_changed_current_state_cannot_borrow_an_unused_old_root_slot(self):
        fixture=self.fixture();state=fixture[1]
        state['ticker']='CHANGED'
        state['state_hash']=content_hash({k:v for k,v in state.items() if k!='state_hash'})
        with self.assertRaises(CockpitModelError):self.run_repair(fixture)
        self.assertEqual(len(fixture[5].served),1)

    def test_unknown_failed_and_inflight_child_consume_original_slot_even_after_policy_change(self):
        for outcome in ('unknown',KeyboardInterrupt('isolated live repair lease')):
            fixture=self.fixture(repair_outcome=outcome);model=fixture[3];adapter=fixture[5]
            with self.subTest(outcome=type(outcome).__name__),self.assertRaises((specs.CompanyModelSpecError,CockpitModelError,KeyboardInterrupt)):
                self.run_repair(fixture)
            self.assertEqual(len(adapter.served),2)
            model.config['purpose_call_budgets']['model_spec']['max_output_tokens']=7000
            with patch.object(model,'call',wraps=model.call) as called,self.assertRaises(specs.CompanyModelSpecError):
                self.run_repair(fixture)
            called.assert_not_called();self.assertEqual(len(adapter.served),2)
