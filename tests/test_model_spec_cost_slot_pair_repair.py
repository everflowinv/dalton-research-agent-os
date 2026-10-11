"""A contradictory template-slot declaration uses one exact-parent correction."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from dataclasses import replace
from dalton_core import company_model_cli as cli, company_model_spec as specs
from dalton_core.model_spec_typed_field_repair import (
    COST_SLOT_PAIR_CODE as CODE, cost_slot_pair_scope, validate_parent,
    validate_frozen_parent, assert_only_typed_fields_changed,
)
from dalton_core.driver_template import cost_slot_ids
from tests import test_company_model_spec as fixtures
from tests import test_model_spec_cash_flow_base_repair as calls
from tests import test_model_spec_it_services_structure as replay
from tests import test_model_spec_typed_repair_authority as authority
from tests import test_model_spec_signed_expense_repair as financial
from tests import test_cockpit_model_fallback as runtime
from dalton_core.scheduler import Scheduler


def case(classification='structural_growth', branch='unbound'):
    state=copy.deepcopy(fixtures.STATE);body=fixtures._spec()
    state['industry_classification']={'classification':classification}
    body['expense_lines'][0].update(cost_driver_slot=cost_slot_ids(classification)[0],
        cost_driver_unbound_reason='The exact source has no disclosed operating quantity or unit cost.')
    fixed=copy.deepcopy(body)
    if branch=='bound':fixed['expense_lines'][0].pop('cost_driver_unbound_reason')
    else:fixed['expense_lines'][0]['cost_driver_slot']=None
    return state,body,fixed


def error_for(state,body):
    try:specs.spec_from_response(state,body,decided_by='automation:test')
    except specs.CompanyModelSpecError as error:return error
    raise AssertionError('expected mutually exclusive pair refusal')


class CostSlotPairRepairTests(unittest.TestCase):
    def test_cross_company_and_all_slot_classes_allow_only_two_explicit_branches(self):
        for classification in ('structural_growth','contract_compounder','commodity_cycle','capital_cycle','turnaround','generic'):
            for branch in ('bound','unbound'):
                state,bad,fixed=case(classification,branch)
                state['company_ref']='company:sec-cik:0000220011' if branch=='bound' else 'company:sec-cik:0000220012'
                with self.subTest(classification=classification,branch=branch):
                    error=error_for(state,bad);self.assertEqual(error.code,CODE)
                    validate_parent(bad,error.proof,state=state)
                    validate_frozen_parent(bad,error.proof,state_hash=state['state_hash'],root_prompt=specs.build_prompt(state))
                    assert_only_typed_fields_changed(bad,fixed,error.proof,state=state)
                    accepted=specs.spec_from_response(state,fixed,decided_by='automation:test')
                    self.assertEqual(accepted['expense_lines'][0].get('cost_driver_slot'),fixed['expense_lines'][0]['cost_driver_slot'])

    def test_unknown_slot_or_isolated_or_invalid_reason_never_qualifies(self):
        for change in ('unknown','isolated','empty','bool','numeric'):
            state,bad,_=case()
            if change=='unknown':bad['expense_lines'][0]['cost_driver_slot']='unregistered-cost'
            if change=='isolated':bad['expense_lines'][0].pop('cost_driver_slot')
            if change=='empty':bad['expense_lines'][0]['cost_driver_unbound_reason']=' '
            if change=='bool':bad['expense_lines'][0]['cost_driver_unbound_reason']=True
            if change=='numeric':bad['expense_lines'][0]['cost_driver_unbound_reason']=1
            with self.subTest(change=change):
                self.assertEqual(error_for(state,bad).code,'semantic')
                self.assertIsNone(cost_slot_pair_scope(state,bad,0))
                class Model:
                    scheduler_db=None
                    def call(self,**kw):raise AssertionError('invalid economic metadata must not buy a repair')
                with self.assertRaises(specs.CompanyModelSpecError):
                    cli._validated_spec_with_repair(model=Model(),state=state,mission={},original_call=calls.root_call(bad),repair_config={'max_attempts':1},decided_by='automation:test')

    def test_no_automatic_deletion_and_cost_template_name_does_not_prove_base(self):
        state,bad,fixed=case();original=copy.deepcopy(bad)
        error=error_for(state,bad)
        self.assertEqual(bad,original)
        self.assertIn('valid slot name alone does not prove',error.proof['detail']['instruction'])
        fixed['expense_lines'][0].pop('cost_driver_unbound_reason')
        with self.assertRaises(specs.CompanyModelSpecError):assert_only_typed_fields_changed(bad,fixed,error.proof,state=state)

    def test_parent_root_state_classification_and_paths_cannot_be_forged(self):
        state,bad,_=case();scope=error_for(state,bad).proof;prompt=specs.build_prompt(state)
        for field in ('body','index','field','classification','state','slot'):
            forged=copy.deepcopy(scope)
            if field=='body':forged['parent_body_hash']='0'*64
            if field=='index':forged['paths'][0][1]=False
            if field=='field':forged['paths'][1][-1]='because'
            if field=='classification':forged['detail']['classification']='contract_compounder'
            if field=='state':forged['detail']['state_hash']='b'*64
            if field=='slot':forged['detail']['original_slot']='delivery_cost'
            with self.subTest(field=field),self.assertRaises(specs.CompanyModelSpecError):
                validate_frozen_parent(bad,forged,state_hash=state['state_hash'],root_prompt=prompt)
        for forged_prompt in (prompt.replace('unit_cost_curve\t','foreign\t'),prompt+ '\nCOST DRIVER TEMPLATE (generic):'):
            with self.assertRaises(specs.CompanyModelSpecError):validate_frozen_parent(bad,scope,state_hash=state['state_hash'],root_prompt=forged_prompt)

    def test_every_unrelated_fact_formula_evidence_and_type_is_exactly_preserved(self):
        state,bad,fixed=case('generic');scope=error_for(state,bad).proof
        for field in ('concept','label','assessment','period','value','formula','source','type','different_slot','order','nonobject'):
            hostile=copy.deepcopy(fixed)
            if field=='concept':hostile['expense_lines'][0]['basis_concept']='unfiled:Cost'
            if field=='label':hostile['expense_lines'][0]['label']='Renamed'
            if field=='assessment':hostile['assessment']='Different'
            if field=='period':hostile['horizon']['historical_quarters']=8
            if field=='value':hostile['expense_lines'][0]['value']='100'
            if field=='formula':hostile['driver_formulas'][0]['operator']='sum'
            if field=='source':hostile['expense_lines'][0]['source_ref']='different:source'
            if field=='type':hostile['operating_metrics'][0]['disclosed']=1
            if field=='different_slot':hostile['expense_lines'][0].update(cost_driver_slot='variable_cost');hostile['expense_lines'][0].pop('cost_driver_unbound_reason')
            if field=='order':hostile['expense_lines'].reverse()
            if field=='nonobject':hostile['expense_lines'][0]=True
            with self.subTest(field=field),self.assertRaises(specs.CompanyModelSpecError):assert_only_typed_fields_changed(bad,hostile,scope,state=state)

    def test_one_correction_keeps_paid_refs_and_revalidates_actual_financial_dag(self):
        for ties in (True,False):
            state,body,inputs=financial.case()
            if ties:body=financial.fixed(body)
            state['industry_classification']='structural_growth'
            body['expense_lines'][0].update(cost_driver_slot='unit_cost_curve',cost_driver_unbound_reason='No filed unit-cost evidence.')
            fixed=copy.deepcopy(body);fixed['expense_lines'][0]['cost_driver_slot']=None
            seen=[];validated=[];attempts=[]
            class Model:
                scheduler_db=None
                def call(self,**kw):seen.append(kw);return {**calls.root_call(fixed),'work_order_ref':'work:paid-correction','cost_micros':53}
            run=lambda:cli._validated_spec_with_repair(model=Model(),state=state,mission={},original_call=calls.root_call(body),repair_config={'max_attempts':1},decided_by='automation:test',repair_attempts=attempts,financial_validator=financial.callback(inputs,validated))
            if ties:run()
            else:
                with self.assertRaises(specs.CompanyModelSpecError):run()
            self.assertEqual(len(seen),1);self.assertEqual(len(validated),1)
            self.assertEqual(attempts[0]['cost_micros'],53);self.assertEqual(attempts[0]['work_order_ref'],'work:paid-correction')
            self.assertIn('never silently erase a real evidence gap',seen[0]['prompt'])

    def test_disabled_or_already_consumed_repair_never_buys_second_correction(self):
        state,bad,fixed=case();calls_seen=[]
        class Model:
            scheduler_db=None
            def call(self,**kw):calls_seen.append(kw);return {**calls.root_call(bad),'cost_micros':17}
        for mode in ('disabled','prior-format','two-pairs'):
            original=calls.root_call(bad);answer=copy.deepcopy(fixed);attempts=[]
            if mode=='prior-format':original={**original,'text':'not json'}
            if mode=='two-pairs':
                bad['expense_lines'][1].update(cost_driver_slot='unit_cost_curve',cost_driver_unbound_reason='No filed cost base.')
                original=calls.root_call(bad);answer=copy.deepcopy(bad);answer['expense_lines'][0]['cost_driver_slot']=None
            calls_seen.clear()
            class Model:
                scheduler_db=None
                def call(self,**kw):calls_seen.append(kw);return {**calls.root_call(answer if mode=='two-pairs'else bad),'cost_micros':17}
            with self.subTest(mode=mode),self.assertRaises(specs.CompanyModelSpecError):
                cli._validated_spec_with_repair(model=Model(),state=state,mission={},original_call=original,repair_config={'max_attempts':0 if mode=='disabled' else 5},decided_by='automation:test',repair_attempts=attempts)
            self.assertEqual(len(calls_seen),0 if mode=='disabled' else 1)
            if mode!='disabled':self.assertEqual(attempts[0]['cost_micros'],17)

    def test_exact_old_paid_correction_replays_free_with_full_validation(self):
        state,bad,fixed=case();error=error_for(state,bad)
        with tempfile.TemporaryDirectory()as d:
            db=Path(d)/'scheduler.sqlite'
            replay._scheduler_db(db,[{'binding':replay._binding(json.dumps(bad),str(error),code=CODE,contract='e'*64),'text':json.dumps(fixed)}])
            before=db.read_bytes();validated=[]
            class Model:
                scheduler_db=db
                def call(self,**kw):raise AssertionError('no duplicate provider call')
            _,attempts,result=cli._validated_spec_with_repair(model=Model(),state=state,mission={},original_call=calls.root_call(bad),repair_config={'max_attempts':0},decided_by='automation:test',financial_validator=lambda spec:validated.append(spec))
            self.assertTrue(result['replayed']);self.assertEqual(attempts[0]['cost_micros'],0);self.assertEqual(len(validated),1);self.assertEqual(db.read_bytes(),before)

    def test_real_runtime_strong_parent_admission_and_repeat_replay(self):
        h=authority.TypedRepairAuthorityTests('runTest');h.addCleanup=self.addCleanup
        for branch in ('bound','unbound'):
            with self.subTest(branch=branch):h._run(lambda:case(branch=branch))

    def test_original_paid_successful_parent_is_not_bought_again_under_new_repair_contract(self):
        fixture=runtime.CockpitChainTests('test_model_spec_repair_is_a_second_budgeted_scheduler_work')
        fixture.setUp();self.addCleanup(fixture.doCleanups)
        state,bad,fixed=case()
        class Adapter(runtime.ChainAdapter):
            def __init__(self):super().__init__({});self.outputs=[bad,fixed]
            def execute(self,work,route,profile):
                invocation,envelope=super().execute(work,route,profile)
                return invocation,replace(envelope,outputs={'text':json.dumps(self.outputs.pop(0))})
        adapter=Adapter();model=fixture._model(adapter,policy_version_ref=fixture.pinned_policy,model_spec_output_tokens=6000)
        kwargs={'purpose':'model_spec','request_id':cli.model_spec_request_id(state['state_hash'],repair_config={'max_attempts':1}),
                'prompt':specs.build_prompt(state),'mission':fixture.mission,
                '_model_spec_request_identity':cli.model_spec_request_identity(state['state_hash'],repair_config={'max_attempts':1})}
        paid=model.call(**kwargs)
        with Scheduler(fixture.root/'scheduler.sqlite')as scheduler:old=scheduler.formal_result(paid['work_order_ref'])
        replayed=model.call(**kwargs)
        self.assertTrue(replayed['replayed']);self.assertEqual(replayed['work_order_ref'],paid['work_order_ref'])
        self.assertEqual(len(adapter.served),1)
        _,attempts,accepted=cli._validated_spec_with_repair(model=model,state=state,mission=fixture.mission,
            original_call=replayed,repair_config={'max_attempts':1},decided_by='automation:test')
        self.assertEqual(len(adapter.served),2);self.assertEqual(len(attempts),1)
        self.assertNotEqual(accepted['work_order_ref'],paid['work_order_ref'])
        with Scheduler(fixture.root/'scheduler.sqlite')as scheduler:self.assertEqual(scheduler.formal_result(paid['work_order_ref']),old)

    def test_rehashed_foreign_scope_is_rejected_before_runtime_provider(self):
        h=authority.TypedRepairAuthorityTests('runTest');h.addCleanup=self.addCleanup
        h._run(case,change=lambda b:b['typed_field_proof']['detail'].update(original_slot='invented'),expected='parent proof differs')

    def test_primary_task_and_prompt_unchanged_only_repair_contract_is_versioned(self):
        self.assertEqual(specs.TASK_HASH,'608338bcd6cf70cbe6e012acd5f579b99d34f9d990d5bc846c4059a198eda9af')
        self.assertEqual(cli.REPAIR_CONTRACT_REF,'contract:company-model-spec-structured-output-repair:0.11')
        state,_,_=case();prompt=specs.build_prompt(state)
        self.assertIn('Bind each proposed expense line to one cost slot, or use null',prompt)
        self.assertEqual(cli.DEFAULT_REPAIR_ATTEMPTS,1)
