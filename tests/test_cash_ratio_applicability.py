import copy
import json
import unittest
from decimal import Decimal
from unittest.mock import patch
from dalton_core import cash_flow_ratio_applicability as rule
from dalton_core import company_model_cli as cli
from dalton_core.company_model_spec import CompanyModelSpecError
from dalton_core.company_financial_statement_structure import materialize_financial_statement_structure, forecast_structure_binding
from dalton_core.model_forecast_driver import (build_cash_flow_companion, build_structured_forecast_model,
    build_structure_drivers, _structure_historical_values, cash_companion_topology_changed)
from dalton_core.store import content_hash, canonical_json
from tests.test_derived_cash_forecast_base import derived_fixture
from tests.test_cash_flow_filed_tie_base import fixture as filed_fixture


def fixture(company='company:test:newco', unit='usd', value='-10'):
    inputs,spec,_,_,_=derived_fixture(company,unit)
    spec.update(state_hash='a'*64,task_hash='b'*64)
    by={l['concept']:l for l in inputs['filed_lines']};end=sorted(by['tax']['cells'])[0]
    amount=Decimal(value);by['tax']['cells'][end]['value']=str(Decimal('190')-amount)
    for concept,val in [('net',amount),('parent',amount-5),('eps_numerator',amount-5),('eps',(amount-5)/100)]:
        by[concept]['cells'][end]['value']=str(val)
    for source in inputs['filed_lines']:
        if source['statement']=='income' and source['period_basis']!='instant':
            source['duration_facts']=[{'period_start':c['period_start'],'period_end':e,'period_kind':'quarter',
                'value':c['value'],'unit':c['unit'],'source_accessions':c['source_accessions'],'source_forms':['10-Q']}
                for e,c in source['cells'].items()]
    spec['content_hash']=content_hash({k:v for k,v in spec.items()if k!='content_hash'})
    structure,replay=materialize_financial_statement_structure(spec,inputs)
    return inputs,spec,structure,replay,forecast_structure_binding(structure,replay,inputs)


def proof_for(inputs,spec,structure):
    try:build_cash_flow_companion(spec,inputs,structure)
    except rule.CashRatioUnavailable as exc:
        error=rule.repair_error(spec,inputs,exc.diagnostic)
        assert error is not None
        return error
    raise AssertionError('method should be inapplicable')


class CashRatioApplicabilityTests(unittest.TestCase):
    def test_two_companies_zero_and_mixed_are_proved_not_missing(self):
        for company,unit,value,reason in [('company:test:co1','usd','0','zero_denominator'),('company:test:co2','eur','-10','mixed_sign_denominator')]:
            inputs,spec,structure,_,_=fixture(company,unit,value)
            before=canonical_json(inputs);error=proof_for(inputs,spec,structure)
            self.assertEqual(error.code,rule.ERROR_CODE)
            self.assertEqual(error.proof['diagnostic']['reason'],reason)
            self.assertEqual(error.proof['diagnostic']['missing_periods'],[])
            self.assertEqual(len(error.proof['diagnostic']['samples']),4)
            self.assertIn('revenue',error.proof['allowed_base_refs'])
            self.assertEqual(canonical_json(inputs),before)

    def test_missing_period_duration_source_and_unit_are_not_semantic_repairs(self):
        for kind in ('period','duration','source','unit'):
            inputs,spec,structure,_,_=fixture()
            ds=build_structure_drivers(inputs,structure);values,refs=_structure_historical_values(ds,structure)
            source=inputs['cash_flow_inputs'][0]
            cash=[{**c,'concept':source['concept'],'accessions':c['source_accessions']}for c in source['series']['quarters']]
            end=cash[-1]['period_end']
            if kind=='period':del values['net'][end]
            if kind=='duration':cash[-1]['period_start']='2025-12-01'
            if kind=='source':refs['net'][end][0]['accession']='foreign'
            if kind=='unit':ds[0]['unit']='eur'
            d=rule.window(cash,values['net'],refs['net'],ds,unit='usd')
            self.assertNotIn(d['reason'],('zero_denominator','mixed_sign_denominator'))
            with self.subTest(kind=kind):
                self.assertIn(d['reason'],('missing_periods','duration_mismatch','source_unproved','unit_mismatch'))

    def test_both_allowed_model_choices_repeat_full_financial_and_keep_cash_gaps_honest(self):
        inputs,spec,structure,_,_=fixture();error=proof_for(inputs,spec,structure)
        for method,base in [('share_of_line','revenue'),('unavailable',None)]:
            raw={'cash_flow_companion':copy.deepcopy(spec['cash_flow_companion']),'unchanged':{'value':'149','source':'original'}}
            changed=copy.deepcopy(raw);changed['cash_flow_companion']['lines'][0].update(forecast_method=method,forecast_base_ref=base,because='Explicit method decision, original cash source unchanged.')
            rule.assert_only_allowed_changed(raw,changed,error.proof)
            candidate=copy.deepcopy(spec);candidate['cash_flow_companion']=changed['cash_flow_companion']
            st,replay=materialize_financial_statement_structure(candidate,inputs)
            body=build_structured_forecast_model(candidate,inputs,structure=st,replay=replay,binding=forecast_structure_binding(st,replay,inputs))
            results={r['ref']:r for r in body['results']}
            self.assertEqual(results['result:operating_cash_flow']['status'],'computed'if base else'unavailable')
            if base is None:
                self.assertEqual(results['result:free_cash_flow']['status'],'unavailable')
                self.assertTrue(all(c['value']is None for c in results['result:operating_cash_flow']['cells']))
            self.assertEqual(results['result:net_income']['status'],'computed')

    def test_scope_rejects_other_fact_formula_source_and_unproved_base_changes(self):
        inputs,spec,structure,_,_=fixture();p=proof_for(inputs,spec,structure).proof
        raw={'cash_flow_companion':copy.deepcopy(spec['cash_flow_companion']),'formula':{'amount':'100','source':'original'}}
        for field in ('amount','source','formula','other_line','unknown_base','income_unavailable','proof'):
            new=copy.deepcopy(raw);new['cash_flow_companion']['lines'][0].update(forecast_method='unavailable',forecast_base_ref=None,because='Gap retained.')
            proof=copy.deepcopy(p)
            if field in ('amount','source'):new['formula'][field]='changed'
            elif field=='formula':new['cash_flow_companion']['formula']['terms'][0]['coefficient']='-1'
            elif field=='other_line':new['cash_flow_companion']['lines'][1]['because']='changed'
            elif field=='unknown_base':new['cash_flow_companion']['lines'][0].update(forecast_method='share_of_line',forecast_base_ref='invented')
            elif field=='income_unavailable':new['cash_flow_companion']['lines'][0].update(forecast_method='share_of_line',forecast_base_ref='long-term-debt')
            else:proof['diagnostic']['reason']='missing_periods'
            with self.subTest(field=field),self.assertRaises(CompanyModelSpecError):rule.assert_only_allowed_changed(raw,new,proof)

    def test_current_root_once_slot_and_free_parent_replay_are_not_new_permissions(self):
        inputs,spec,structure,_,_=fixture();error=proof_for(inputs,spec,structure)
        from tests.test_model_spec_cash_flow_base_repair import root_call
        raw={'cash_flow_companion':copy.deepcopy(spec['cash_flow_companion'])};fixed=copy.deepcopy(raw)
        fixed['cash_flow_companion']['lines'][0].update(forecast_method='unavailable',forecast_base_ref=None,because='Cash ratio is unsuitable; forecast remains missing.')
        class Model:
            scheduler_db=None
            calls=[]
            def call(self,**kwargs):self.calls.append(kwargs);return {**root_call(fixed),'cost_micros':47}
        def parse(_,text,**kwargs):
            candidate=copy.deepcopy(spec);candidate['cash_flow_companion']=json.loads(text)['cash_flow_companion'];return candidate
        def validate(candidate):
            st,_=materialize_financial_statement_structure(candidate,inputs)
            try:build_cash_flow_companion(candidate,inputs,st)
            except rule.CashRatioUnavailable as e:raise rule.repair_error(candidate,inputs,e.diagnostic)
        model=Model();attempts=[]
        with patch.object(cli,'spec_from_response',side_effect=parse):
            result,_,_=cli._validated_spec_with_repair(model=model,state={'state_hash':'a'*64},mission={},original_call=root_call(raw),repair_config={'max_attempts':1},decided_by='automation:test',repair_attempts=attempts,financial_validator=validate)
        self.assertEqual(len(model.calls),1);self.assertEqual(attempts[0]['cost_micros'],47)
        self.assertEqual(model.calls[0]['_structured_output_repair']['cash_ratio_proof'],error.proof)
        self.assertEqual(result['cash_flow_companion']['lines'][0]['forecast_method'],'unavailable')
        for disabled,used in [(True,False),(False,True)]:
            model.calls=[]
            with patch.object(cli,'spec_from_response',side_effect=parse),patch.object(cli,'_complete_bundle_slot_used',return_value=used),self.assertRaises(CompanyModelSpecError):
                cli._validated_spec_with_repair(model=model,state={'state_hash':'a'*64},mission={},original_call=root_call(raw),repair_config={'max_attempts':0 if disabled else 1},decided_by='automation:test',financial_validator=validate)
            self.assertEqual(model.calls,[])
        prior={**root_call(fixed),'repair_number':1,'binding_hash':'a'*64,'repair_contract_hash':'b'*64}
        model.calls=[]
        with patch.object(cli,'spec_from_response',side_effect=parse),patch.object(cli,'_prior_contract_repair',return_value=prior):
            result,attempts,_=cli._validated_spec_with_repair(model=model,state={'state_hash':'a'*64},mission={},original_call=root_call(raw),repair_config={'max_attempts':0},decided_by='automation:test',financial_validator=validate)
        self.assertEqual(model.calls,[]);self.assertTrue(attempts[0]['replayed']);self.assertEqual(attempts[0]['cost_micros'],0)

    def test_basis_contract_change_is_distinct_but_source_only_update_is_not(self):
        inputs,spec,structure,_,_,_=filed_fixture()
        prior={'cash_flow_companion':build_cash_flow_companion(spec,inputs,structure)}
        self.assertFalse(cash_companion_topology_changed(prior,spec,inputs,structure))
        # Restore the missing original component: the independently reported
        # denominator is no longer needed. It is a new contract, not an alias.
        cost=next(s for s in inputs['filed_lines']if s['concept']=='cost')
        first=min(next(s for s in inputs['filed_lines']if s['concept']=='revenue')['cells'])
        template=copy.deepcopy(cost['cells'][sorted(cost['cells'])[0]]);template.update(period_start='2025-01-01',value='600')
        cost['cells'][first]=template
        structure,_=materialize_financial_statement_structure(spec,inputs)
        self.assertTrue(cash_companion_topology_changed(prior,spec,inputs,structure))

    def test_normal_lane_rebuilds_after_basis_change_without_actualize_or_spec_purchase(self):
        import sqlite3
        from dalton_core import company_model_forecast as lane
        inputs,spec,structure,replay,binding,_=filed_fixture()
        prior=build_structured_forecast_model(spec,inputs,structure=structure,replay=replay,binding=binding)
        prior.update(id='model:old',version=1)
        cost=next(x for x in inputs['filed_lines']if x['concept']=='cost')
        first=min(next(x for x in inputs['filed_lines']if x['concept']=='revenue')['cells'])
        c=copy.deepcopy(cost['cells'][sorted(cost['cells'])[0]]);c.update(period_start='2025-01-01',value='600');cost['cells'][first]=c
        class Models:
            def latest(self,company):return prior
        models=Models();models.connection=sqlite3.connect(':memory:')
        self.addCleanup(models.connection.close)
        with patch.object(lane,'build_model_inputs',return_value=inputs),patch.object(lane,'coverage_sources',return_value={}),patch.object(lane,'forecast_reporting_boundary',return_value={'fiscal_year_end':'12-31'}),patch('dalton_core.forecast_company_fcf.load_company_fcf_definition',return_value=None),patch.object(lane,'actualize_model',side_effect=AssertionError('basis change is not actualize')),patch.object(lane,'build_structured_forecast_model',side_effect=RuntimeError('normal fresh builder reached')):
            with self.assertRaisesRegex(RuntimeError,'normal fresh builder reached'):
                lane.run_company_forecast(object(),spec,models=models)

    def test_cash_unit_failure_and_capex_inapplicability_do_not_open_ocf_repair(self):
        inputs,spec,structure,_,_=fixture()
        inputs['cash_flow_inputs'][0]['series']['quarters'][0]['unit']='eur'
        with self.assertRaisesRegex(rule.ForecastModelUnavailable,'source quarter units'):
            build_cash_flow_companion(spec,inputs,structure)
        inputs,spec,structure,_,_=fixture()
        spec['cash_flow_companion']['lines'][0].update(forecast_method='unavailable',forecast_base_ref=None)
        spec['cash_flow_companion']['lines'][1]['forecast_base_ref']='net'
        with self.assertRaises(rule.CashRatioUnavailable)as raised:build_cash_flow_companion(spec,inputs,structure)
        self.assertEqual(raised.exception.diagnostic['role'],'capital_expenditure')
        self.assertIsNone(rule.repair_error(spec,inputs,raised.exception.diagnostic))

    def test_wrong_operand_period_is_source_refusal_even_with_mixed_amounts(self):
        inputs,spec,structure,_,_=fixture();drivers=build_structure_drivers(inputs,structure)
        values,refs=_structure_historical_values(drivers,structure);cash=inputs['cash_flow_inputs'][0]
        cells=[{**c,'concept':cash['concept'],'accessions':c['source_accessions']}for c in cash['series']['quarters']]
        refs['net'][cells[0]['period_end']][0]['period_end']='2099-03-31'
        self.assertEqual(rule.window(cells,values['net'],refs['net'],drivers,unit='usd')['reason'],'source_unproved')

    def test_normal_financial_authority_translates_only_the_reconstructed_source_diagnostic(self):
        from dalton_core.coverage_mission import CoverageMissionAuthority,company_model_spec_id
        from dalton_core.model_spec_signed_expense_repair import validate_financials_for_repair
        inputs,spec,_,_,_=fixture()
        inputs['spec_ref']=company_model_spec_id(spec['company_ref'],spec['state_hash'],spec['task_hash'])
        class Missions:
            validate_company_model_spec_financials=CoverageMissionAuthority.validate_company_model_spec_financials
        with patch('dalton_core.company_model_inputs.build_model_inputs',return_value=inputs):
            with self.assertRaises(CompanyModelSpecError)as raised:validate_financials_for_repair(Missions(),spec)
        self.assertEqual(raised.exception.code,rule.ERROR_CODE)
        self.assertEqual(raised.exception.proof['financial_input_hash'],content_hash(inputs))
        self.assertEqual(raised.exception.proof['diagnostic']['missing_periods'],[])

    def test_proof_hash_and_binding_state_cannot_be_changed_by_the_repaired_answer(self):
        inputs,spec,structure,_,_=fixture();error=proof_for(inputs,spec,structure)
        raw={'cash_flow_companion':copy.deepcopy(spec['cash_flow_companion'])}
        forged=copy.deepcopy(error.proof);forged['allowed_base_refs'].append('unproved')
        with self.assertRaises(CompanyModelSpecError):rule.validate_parent(raw,forged)
        parent=copy.deepcopy(raw);parent['cash_flow_companion']['lines'][0]['concept']='foreign:cash'
        with self.assertRaises(CompanyModelSpecError):rule.validate_parent(parent,error.proof)

    def test_an_individually_proved_history_with_no_future_base_is_not_a_choice(self):
        inputs,spec,structure,_,_=fixture()
        next(l for l in spec['financial_statement_structure']['lines']if l['ref']=='nci')['forecast_method']='unavailable'
        structure,_=materialize_financial_statement_structure(spec,inputs)
        error=proof_for(inputs,spec,structure)
        self.assertNotIn('nci',error.proof['allowed_base_refs'])
