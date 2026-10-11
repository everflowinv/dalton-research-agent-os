"""Only a known unsupported OCF base may be repaired to an explicit gap."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from dalton_core import company_model_cli as cli, company_model_spec as specs
from tests import test_model_spec_eps_numerator_bridge as bridge
from tests import test_model_spec_it_services_structure as replay

CODE = specs.CASH_FLOW_OPERATING_BASE_SHAPE_CODE
MESSAGE = 'cash_flow_companion operating_cash_flow base is not a forecastable filed line'
OCF = 'us-gaap:NetCashProvidedByUsedInOperatingActivities'
CAPEX = 'us-gaap:PaymentsToAcquirePropertyPlantAndEquipment'


def proposal():
    state, body = bridge.model_reply()
    state['concepts'].extend([OCF, CAPEX])
    state['statements']['cash'] = [{'concept':c,'label':c,'level':0,'parent_concept':None,
        'is_breakdown':False,'dimension_axis':None,'unit':'USD','period_kind':'duration'}for c in (OCF,CAPEX)]
    for statement in body['forecast_statements']:
        if statement['statement']=='cash':statement['importance']='required'
    body['cash_flow_companion']['lines'][0].update(concept=OCF,forecast_method='share_of_line',
        forecast_base_ref='net',because='OCF was incorrectly declared against a derived income subtotal.')
    body['cash_flow_companion']['lines'][1]['concept']=CAPEX
    return state, body


def unavailable(body):
    result=copy.deepcopy(body)
    result['cash_flow_companion']['lines'][0].update(forecast_method='unavailable',forecast_base_ref=None,
        because='The declared derived subtotal is not a supported cash forecast base; OCF remains unavailable.')
    return result


def root_call(body):
    return {'text':json.dumps(body),'work_order_ref':'work:root','work_order_hash':'1'*64,
        'result_envelope_ref':'result:root','result_envelope_hash':'2'*64,
        'invocation_ref':'invocation:root','route_decision_ref':'route:root'}


class CashFlowBaseRepairTests(unittest.TestCase):
    def test_real_parser_typed_error_and_explicit_gap_preserves_source(self):
        state,bad=proposal()
        with self.assertRaises(specs.CompanyModelSpecError)as raised:
            specs.spec_from_response(state,json.dumps(bad),decided_by='automation:test')
        self.assertEqual((raised.exception.code,str(raised.exception)),(CODE,MESSAGE))
        repaired=unavailable(bad)
        spec=specs.spec_from_response(state,json.dumps(repaired),decided_by='automation:test')
        self.assertEqual(spec['cash_flow_companion']['lines'][0]['concept'],OCF)
        self.assertEqual(spec['cash_flow_companion']['lines'][0]['forecast_method'],'unavailable')
        self.assertEqual(spec['cash_flow_companion']['formula'],bad['cash_flow_companion']['formula'])
        cli._assert_cash_flow_operating_base_only_changed(bad,repaired)

    def test_one_normal_repair_and_accounting_references_are_preserved(self):
        state,bad=proposal();fixed=unavailable(bad)
        class Model:
            scheduler_db=None
            calls=[]
            def call(self,**kwargs):
                self.calls.append(kwargs)
                return {**root_call(fixed),'work_order_ref':'work:repair','cost_micros':47}
        model=Model();attempts=[]
        spec,_,call=cli._validated_spec_with_repair(model=model,state=state,mission={},original_call=root_call(bad),
            repair_config={'max_attempts':1},decided_by='automation:test',repair_attempts=attempts)
        self.assertEqual(len(model.calls),1)
        self.assertEqual(model.calls[0]['_structured_output_repair']['validation_error']['code'],CODE)
        self.assertIn('Do not substitute a revenue',model.calls[0]['prompt'])
        self.assertEqual(attempts[0]['cost_micros'],47)
        self.assertEqual(call['work_order_ref'],'work:repair')
        self.assertEqual(spec['cash_flow_companion']['lines'][0]['forecast_method'],'unavailable')

    def test_failed_repair_keeps_paid_attempt_and_cannot_start_second_repair(self):
        state,bad=proposal()
        class Model:
            scheduler_db=None
            calls=0
            def call(self,**kwargs):
                self.calls+=1
                return {**root_call(bad),'cost_micros':47}
        model=Model();attempts=[]
        with self.assertRaisesRegex(specs.CompanyModelSpecError,'outside its exact scope'):
            cli._validated_spec_with_repair(model=model,state=state,mission={},original_call=root_call(bad),
                repair_config={'max_attempts':1},decided_by='automation:test',repair_attempts=attempts)
        self.assertEqual(model.calls,1);self.assertEqual(attempts[0]['cost_micros'],47)

    def test_semantic_errors_remain_ineligible_and_disabled_means_zero_calls(self):
        for change in ['disabled','foreign_concept','missing_base','capex_base']:
            state,body=proposal()
            if change=='foreign_concept':body['cash_flow_companion']['lines'][0]['concept']='invented:OCF'
            if change=='missing_base':body['cash_flow_companion']['lines'][0]['forecast_base_ref']='not-present'
            if change=='capex_base':
                body['cash_flow_companion']['lines'][0]=unavailable(body)['cash_flow_companion']['lines'][0]
                body['cash_flow_companion']['lines'][1].update(forecast_method='share_of_line',forecast_base_ref='net')
            class Model:
                scheduler_db=None
                def call(self,**kwargs):raise AssertionError('no repair call authorized')
            with self.subTest(change=change),self.assertRaises(specs.CompanyModelSpecError)as raised:
                cli._validated_spec_with_repair(model=Model(),state=state,mission={},original_call=root_call(body),
                    repair_config={'max_attempts':0 if change=='disabled'else 1},decided_by='automation:test')
            self.assertEqual(raised.exception.code,CODE if change=='disabled'else'semantic')

    def test_scope_rejects_other_changes_even_when_full_spec_would_accept(self):
        _,original=proposal()
        for change in ['new_base','source','income','capital_expenditure','fcf','order','horizon']:
            fixed=unavailable(original)
            if change=='new_base':fixed['cash_flow_companion']['lines'][0].update(forecast_method='share_of_line',forecast_base_ref='revenue')
            if change=='source':fixed['cash_flow_companion']['lines'][0]['concept']=None
            if change=='income':fixed['financial_statement_structure']['lines'][0]['label']='Changed valid label'
            if change=='capital_expenditure':fixed['cash_flow_companion']['lines'][1]['because']='Different assumption'
            if change=='fcf':fixed['cash_flow_companion']['formula']['terms'][1]['coefficient']='1'
            if change=='order':fixed['cash_flow_companion']['lines'].reverse()
            if change=='horizon':fixed['horizon']['forecast_quarters']=12
            with self.subTest(change=change),self.assertRaisesRegex(specs.CompanyModelSpecError,'outside its exact scope'):
                cli._assert_cash_flow_operating_base_only_changed(original,fixed)

    def test_old_same_task_repair_is_scope_checked_and_revalidated_without_calls(self):
        state,bad=proposal();fixed=unavailable(bad);original=json.dumps(bad)
        with tempfile.TemporaryDirectory()as d:
            db=Path(d)/'scheduler.sqlite'
            replay._scheduler_db(db,[{'binding':replay._binding(original,MESSAGE,code=CODE,contract='e'*64),'text':json.dumps(fixed)}])
            before=db.read_bytes()
            class Model:
                scheduler_db=db
                def call(self,**kwargs):raise AssertionError('held answer is free')
            spec,attempts,call=cli._validated_spec_with_repair(model=Model(),state=state,mission={},
                original_call=root_call(bad),repair_config={'max_attempts':0},decided_by='automation:test')
            self.assertTrue(call['replayed']);self.assertEqual(attempts[0]['cost_micros'],0)
            self.assertEqual(db.read_bytes(),before)
            self.assertEqual(spec['cash_flow_companion']['lines'][0]['forecast_method'],'unavailable')

    def test_cash_engine_keeps_missing_forecast_explicit_and_filed_source_intact(self):
        from tests.test_structured_cash_flow_companion import authorities
        from dalton_core.model_forecast_driver import build_structured_forecast_model
        inputs,spec,structure,replay,binding=authorities()
        derived=next(line for line in structure['lines']if line['role']=='operating_income')
        self.assertEqual(derived['kind'],'derived')
        spec['cash_flow_companion']['lines'][0]['forecast_base_ref']=derived['ref']
        unsupported=build_structured_forecast_model(spec,inputs,structure=structure,replay=replay,binding=binding)
        ocf=next(r for r in unsupported['results']if r['ref']=='result:operating_cash_flow')
        self.assertEqual(ocf['status'],'unavailable')
        self.assertTrue(all(c['value']is None for c in ocf['cells']))
        spec['cash_flow_companion']['lines'][0].update(forecast_method='unavailable',forecast_base_ref=None)
        gap=build_structured_forecast_model(spec,inputs,structure=structure,replay=replay,binding=binding)
        source=gap['cash_flow_companion']['lines'][0]
        self.assertEqual(source['source'],inputs['cash_flow_inputs'][0])
        for ref in ['result:operating_cash_flow','result:free_cash_flow']:
            result=next(r for r in gap['results']if r['ref']==ref)
            self.assertEqual(result['status'],'unavailable')
            self.assertTrue(all(c['value']is None for c in result['cells']))

    def test_a_new_error_after_ocf_repair_cannot_open_another_paid_window(self):
        state,bad=proposal();bad['assessment']='x'*(specs.MAX_ASSESSMENT_CHARS+1);fixed=unavailable(bad)
        class Model:
            scheduler_db=None
            calls=0
            def call(self,**kwargs):
                self.calls+=1
                return {**root_call(fixed),'cost_micros':47}
        model=Model();attempts=[]
        with self.assertRaises(specs.CompanyModelSpecError)as raised:
            cli._validated_spec_with_repair(model=model,state=state,mission={},original_call=root_call(bad),
                repair_config={'max_attempts':5},decided_by='automation:test',repair_attempts=attempts)
        self.assertEqual(raised.exception.code,'text_length')
        self.assertEqual(model.calls,1);self.assertEqual(attempts[0]['cost_micros'],47)

    def test_scope_guard_rejection_keeps_exact_repair_execution_references(self):
        state,bad=proposal();hostile=unavailable(bad)
        hostile['cash_flow_companion']['lines'][0]['concept']=None
        class Model:
            scheduler_db=None
            def call(self,**kwargs):
                return {**root_call(hostile),'work_order_ref':'work:repair',
                    'result_envelope_ref':'result:repair','invocation_ref':'invocation:repair',
                    'route_decision_ref':'route:repair','cost_micros':47}
        attempts=[]
        with self.assertRaisesRegex(specs.CompanyModelSpecError,'outside its exact scope'):
            cli._validated_spec_with_repair(model=Model(),state=state,mission={},original_call=root_call(bad),
                repair_config={'max_attempts':1},decided_by='automation:test',repair_attempts=attempts)
        self.assertEqual(len(attempts),1)
        self.assertEqual({k:attempts[0][k]for k in ['work_order_ref','result_envelope_ref','invocation_ref','route_decision_ref','cost_micros']},
            {'work_order_ref':'work:repair','result_envelope_ref':'result:repair','invocation_ref':'invocation:repair',
             'route_decision_ref':'route:repair','cost_micros':47})

    def test_contract_binds_exact_scoped_repair_and_net_income_source_carry(self):
        from dalton_core.company_financial_statement_structure import NET_INCOME_SOURCE_CARRY_VALIDATION_REF
        self.assertEqual(cli.REPAIR_CONTRACT_REF,'contract:company-model-spec-structured-output-repair:0.9')
        self.assertEqual(cli.model_spec_validation_contract()['net_income_source_carry'],NET_INCOME_SOURCE_CARRY_VALIDATION_REF)
        self.assertEqual(cli.REPAIR_CONTRACT['cash_flow_operating_base_shape']['allowed_fields'],
            ['forecast_method','forecast_base_ref','because'])
        self.assertEqual(cli.DEFAULT_REPAIR_ATTEMPTS,1)
