"""Finite source-bound field corrections keep paid and financial authority intact."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from dalton_core import company_model_spec as specs, company_model_cli as cli
from dalton_core.model_spec_typed_field_repair import (
    ERROR_CODES, IDENTITY_CODE, PERIODICITY_CODE, ACQUISITIONS_CODE,
    assert_only_typed_fields_changed, validate_parent,
)
from dalton_core.company_model_identity_catalog import IDENTITY_CATALOG_REF, cell_identity_ref
from dalton_core.model_revenue_group_candidates import candidates
from tests import test_company_model_spec as fixtures
from tests import test_model_revenue_group_candidates as groups_fixture
from tests import test_model_spec_it_services_structure as replay_fixture
from tests import test_model_spec_cash_flow_base_repair as cash_fixture


def periodicity():
    state=copy.deepcopy(fixtures.STATE);body=fixtures._spec()
    body['operating_metrics'][0]['periodicity']='semiannual'
    fixed=copy.deepcopy(body);fixed['operating_metrics'][0]['periodicity']='annual'
    return state,body,fixed


def acquisitions():
    state,body=cash_fixture.proposal()
    body=cash_fixture.unavailable(body)
    concept='us-gaap:PaymentsToAcquireBusinessesNetOfCashAcquired'
    state['concepts'].append(concept)
    state['statements']['cash'].append({'concept':concept,'label':'Acquisitions',
        'level':0,'parent_concept':None,'is_breakdown':False,'dimension_axis':None,
        'unit':'USD','period_kind':'duration'})
    body['financial_statement_structure']['schema_version']='0.6'
    for line in body['financial_statement_structure']['lines']:
        line.setdefault('annual_forecast_method', 'day_weighted_quarters' if line['role']=='diluted_weighted_average_shares' else None)
    body['financial_statement_structure']['lines'].append({
        'ref':'acquisitions','role':'acquisitions','label':'Acquisitions','kind':'filed',
        'concept':concept,'statement':'cash','unit':'usd','period_kind':'duration',
        'annual_semantics':'sum_quarters','forecast_method':'zero_one_off','forecast_base_ref':None,'annual_forecast_method':None})
    fixed=copy.deepcopy(body);fixed['financial_statement_structure']['lines'][-1]['forecast_method']='unavailable'
    return state,body,fixed


def identities(company='company:sec-cik:0001467373'):
    state=copy.deepcopy(fixtures.STATE);state['company_ref']=company;body=fixtures._spec()
    cells=[groups_fixture.cell('total',100),groups_fixture.cell('east',40,{'region':'east'}),
           groups_fixture.cell('west',60,{'region':'west'})]
    state['numeric_context']={'identity_catalog_contract':IDENTITY_CATALOG_REF,
        'cells':cells,'revenue_group_candidates':candidates(cells)}
    for n,d in enumerate(body['revenue_drivers']):
        d.update(kind='segment',basis_concept='us-gaap:Revenues',unit='USD')
    body['driver_formulas'][0]['operator']='sum'
    fixed=copy.deepcopy(body)
    for d,c in zip(fixed['revenue_drivers'],cells[1:]):
        d['basis_identity']={'statement':'income','dimensions':{'catalog_ref':cell_identity_ref(c)}}
    return state,body,fixed


def error_for(state,body):
    try:specs.spec_from_response(state,body,decided_by='automation:test')
    except specs.CompanyModelSpecError as error:return error
    raise AssertionError('expected precise refusal')


class TypedFieldRepairTests(unittest.TestCase):
    def test_actual_parser_marks_only_three_exact_shapes_and_preserves_legacy_task(self):
        for build,code in [(periodicity,PERIODICITY_CODE),(acquisitions,ACQUISITIONS_CODE),(identities,IDENTITY_CODE)]:
            with self.subTest(code=code):
                state,bad,fixed=build();error=error_for(state,bad)
                self.assertEqual(error.code,code)
                validate_parent(bad,error.proof,state=state)
                assert_only_typed_fields_changed(bad,fixed,error.proof,state=state)
                specs.spec_from_response(state,fixed,decided_by='automation:test')
        self.assertEqual(specs.assessment_compatibility_contract()['current_task_hash'],specs.TASK_HASH)
        self.assertEqual(specs.TASK_HASH,'608338bcd6cf70cbe6e012acd5f579b99d34f9d990d5bc846c4059a198eda9af')
        self.assertIn('typed_field_repair',cli.model_spec_validation_contract())
        self.assertEqual(cli.DEFAULT_REPAIR_ATTEMPTS,1)

    def test_new_source_gate_cross_company_no_label_guessing_and_old_input_replays(self):
        for company in ['company:sec-cik:0000200001','company:sec-cik:0000200002']:
            state,bad,fixed=identities(company);bad['revenue_drivers'][0]['label']='Unrelated label'
            self.assertEqual(error_for(state,bad).code,IDENTITY_CODE)
            legacy=copy.deepcopy(state);legacy['numeric_context'].pop('revenue_group_candidates')
            specs.spec_from_response(legacy,bad,decided_by='automation:test')
        state,bad,fixed=identities()
        fixed['revenue_drivers'][1]['basis_identity']=copy.deepcopy(fixed['revenue_drivers'][0]['basis_identity'])
        self.assertEqual(error_for(state,fixed).code,IDENTITY_CODE)
        fixed['revenue_drivers'][0]['basis_identity']['dimensions']={'catalog_ref':cell_identity_ref(state['numeric_context']['cells'][0])}
        self.assertEqual(error_for(state,fixed).code,IDENTITY_CODE)

    def test_missing_or_forged_source_groups_are_not_a_paid_repair(self):
        for change in ['missing_member','forged_marker','foreign_catalog','foreign_concept']:
            state,bad,fixed=identities()
            if change=='missing_member':
                state['numeric_context']['cells']=state['numeric_context']['cells'][:1]
                state['numeric_context']['revenue_group_candidates']=candidates(state['numeric_context']['cells'])
            if change=='forged_marker':state['numeric_context']['revenue_group_candidates']['groups'][0]['member_sum']='101'
            if change=='foreign_catalog':fixed['revenue_drivers'][0]['basis_identity']['dimensions']['catalog_ref']='foreign';bad=fixed
            if change=='foreign_concept':bad['revenue_drivers'][0]['basis_concept']='unfiled:Revenue'
            err=error_for(state,bad)
            self.assertEqual(err.code,IDENTITY_CODE if change=='foreign_catalog'else'semantic')

    def test_one_paid_correction_is_fully_revalidated_and_accounts_before_rejection(self):
        for build in [periodicity,acquisitions,identities]:
            state,bad,fixed=build();seen=[];validated=[]
            class Model:
                scheduler_db=None
                def call(self,**kw):
                    seen.append(kw);return {**cash_fixture.root_call(fixed),'cost_micros':73,'work_order_ref':'work:repair'}
            out,attempts,call=cli._validated_spec_with_repair(model=Model(),state=state,mission={},
                original_call=cash_fixture.root_call(bad),repair_config={'max_attempts':1},decided_by='automation:test',
                financial_validator=lambda spec:validated.append(spec))
            self.assertEqual(len(seen),1);self.assertEqual(len(validated),1)
            self.assertEqual(attempts[0]['cost_micros'],73);self.assertIn('TYPED_FIELD_PROOF',seen[0]['prompt'])
            self.assertIn('Do not change any other field',seen[0]['prompt'])
            hostile=copy.deepcopy(fixed);hostile['horizon']['forecast_quarters']=9
            class Hostile:
                scheduler_db=None
                def call(self,**kw):return {**cash_fixture.root_call(hostile),'cost_micros':73}
            attempts=[]
            with self.assertRaisesRegex(specs.CompanyModelSpecError,'unrelated field'):
                cli._validated_spec_with_repair(model=Hostile(),state=state,mission={},original_call=cash_fixture.root_call(bad),
                    repair_config={'max_attempts':1},decided_by='automation:test',repair_attempts=attempts)
            self.assertEqual(attempts[0]['cost_micros'],73)

    def test_source_amount_concept_formula_and_other_line_changes_refused(self):
        state,bad,fixed=acquisitions();scope=error_for(state,bad).proof
        for field,value in [('concept','invented:Cash'),('unit','shares'),('ref','different'),('label','Renamed')]:
            hostile=copy.deepcopy(fixed);hostile['financial_statement_structure']['lines'][-1][field]=value
            with self.subTest(field=field),self.assertRaises(specs.CompanyModelSpecError):
                assert_only_typed_fields_changed(bad,hostile,scope,state=state)
        for name in ['income','period','company','evidence']:
            hostile=copy.deepcopy(fixed)
            if name=='income':hostile['financial_statement_structure']['lines'][0]['forecast_method']='unavailable'
            if name=='period':hostile['horizon']['historical_quarters']=8
            if name=='company':hostile['assessment']='Different'
            if name=='evidence':hostile['financial_statement_structure']['formulas'][0]['evidence_refs']=[]
            with self.subTest(name=name),self.assertRaises(specs.CompanyModelSpecError):
                assert_only_typed_fields_changed(bad,hostile,scope,state=state)

    def test_zero_cap_and_already_used_correction_never_open_extra_window(self):
        state,bad,fixed=periodicity()
        class Model:
            scheduler_db=None
            count=0
            def call(self,**kw):
                self.count+=1;return {**cash_fixture.root_call(fixed),'cost_micros':11}
        zero=Model()
        with self.assertRaises(specs.CompanyModelSpecError):
            cli._validated_spec_with_repair(model=zero,state=state,mission={},original_call=cash_fixture.root_call(bad),
                repair_config={'max_attempts':0},decided_by='automation:test')
        self.assertEqual(zero.count,0)
        bad['operating_metrics'][1]['periodicity']='monthly';fixed['operating_metrics'][1]['periodicity']='monthly'
        one=Model();attempts=[]
        with self.assertRaises(specs.CompanyModelSpecError)as raised:
            cli._validated_spec_with_repair(model=one,state=state,mission={},original_call=cash_fixture.root_call(bad),
                repair_config={'max_attempts':5},decided_by='automation:test',repair_attempts=attempts)
        self.assertEqual(raised.exception.code,PERIODICITY_CODE);self.assertEqual(one.count,1)
        self.assertEqual(attempts[0]['cost_micros'],11)

    def test_old_exact_paid_correction_free_replay_is_guarded_and_validated(self):
        state,bad,fixed=periodicity();error=error_for(state,bad);original=json.dumps(bad)
        with tempfile.TemporaryDirectory()as d:
            db=Path(d)/'scheduler.sqlite'
            replay_fixture._scheduler_db(db,[{'binding':replay_fixture._binding(original,str(error),code=error.code,contract='e'*64),'text':json.dumps(fixed)}])
            before=db.read_bytes();validated=[]
            class Model:
                scheduler_db=db
                def call(self,**kw):raise AssertionError('no paid repeat')
            _,attempts,call=cli._validated_spec_with_repair(model=Model(),state=state,mission={},original_call=cash_fixture.root_call(bad),
                repair_config={'max_attempts':0},decided_by='automation:test',financial_validator=lambda spec:validated.append(spec))
            self.assertTrue(call['replayed']);self.assertEqual(attempts[0]['cost_micros'],0)
            self.assertEqual(len(validated),1);self.assertEqual(db.read_bytes(),before)

    def test_full_financial_error_stays_refused_after_legal_field_fix(self):
        state,bad,fixed=identities();calls=[]
        class Model:
            scheduler_db=None
            def call(self,**kw):calls.append(kw);return {**cash_fixture.root_call(fixed),'cost_micros':19}
        def refuse(spec):raise specs.CompanyModelSpecError('original filed arithmetic still does not tie')
        attempts=[]
        with self.assertRaisesRegex(specs.CompanyModelSpecError,'still does not tie'):
            cli._validated_spec_with_repair(model=Model(),state=state,mission={},original_call=cash_fixture.root_call(bad),
                repair_config={'max_attempts':1},decided_by='automation:test',financial_validator=refuse,repair_attempts=attempts)
        self.assertEqual(len(calls),1);self.assertEqual(attempts[0]['cost_micros'],19)

    def test_forged_parent_paths_bool_index_and_wrong_role_refused(self):
        state,bad,fixed=acquisitions();scope=error_for(state,bad).proof
        for hostile in [dict(scope,parent_body_hash='0'*64),dict(scope,paths=[['assessment']]),
                        dict(scope,paths=[['financial_statement_structure','lines',True,'forecast_method']])]:
            with self.assertRaises(specs.CompanyModelSpecError):validate_parent(bad,hostile,state=state)
        bad['financial_statement_structure']['lines'][-1]['role']='capital_expenditure'
        with self.assertRaises(specs.CompanyModelSpecError):validate_parent(bad,scope,state=state)


    def test_new_marker_keeps_complete_direct_identity_and_rejects_equal_presentations(self):
        state,bad,fixed=identities()
        for d,c in zip(fixed['revenue_drivers'],state['numeric_context']['cells'][1:]):
            d['basis_identity']={'statement':'income','dimensions':copy.deepcopy(c['dimensions'])}
        specs.spec_from_response(state,fixed,decided_by='automation:test')
        # Equal duplicate source presentations are a single economic identity.
        alias=copy.deepcopy(state['numeric_context']['cells'][1]);alias.update(line_id='alias',line_content_hash='d'*64)
        state['numeric_context']['cells'].append(alias)
        state['numeric_context']['revenue_group_candidates']=candidates(state['numeric_context']['cells'])
        fixed['revenue_drivers'][1]['basis_identity']={'statement':'income','dimensions':{'catalog_ref':cell_identity_ref(alias)}}
        self.assertEqual(error_for(state,fixed).code,IDENTITY_CODE)
        # A fixed dimension cannot be silently dropped from a complete source identity.
        for c in state['numeric_context']['cells'][1:]:c['dimensions']['scope']='operating'
        state['numeric_context']['cells'].append(groups_fixture.cell('ops',100,{'scope':'operating'}))
        state['numeric_context']['revenue_group_candidates']=candidates(state['numeric_context']['cells'])
        self.assertEqual(error_for(state,fixed).code,IDENTITY_CODE)

    def test_mixed_source_groups_and_periods_rejected_even_when_each_member_is_real(self):
        state,bad,fixed=identities()
        extra=[groups_fixture.cell('product-a',40,{'product':'a'}),groups_fixture.cell('product-b',60,{'product':'b'})]
        state['numeric_context']['cells'].extend(extra)
        state['numeric_context']['revenue_group_candidates']=candidates(state['numeric_context']['cells'])
        fixed['revenue_drivers'][1]['basis_identity']['dimensions']['catalog_ref']=cell_identity_ref(extra[1])
        self.assertEqual(error_for(state,fixed).code,IDENTITY_CODE)
        validated=[];calls=[]
        class Model:
            scheduler_db=None
            def call(self,**kw):calls.append(kw);return {**cash_fixture.root_call(fixed),'cost_micros':37}
        with self.assertRaises(specs.CompanyModelSpecError):
            cli._validated_spec_with_repair(model=Model(),state=state,mission={},original_call=cash_fixture.root_call(bad),
                repair_config={'max_attempts':1},decided_by='automation:test',financial_validator=lambda spec:validated.append(spec))
        self.assertEqual(len(calls),1);self.assertEqual(validated,[])

    def test_existing_paid_format_correction_exhausts_the_single_field_window(self):
        state,bad,fixed=periodicity();calls=[]
        class Model:
            scheduler_db=None
            def call(self,**kw):calls.append(kw);return {**cash_fixture.root_call(bad),'cost_micros':17}
        initial={**cash_fixture.root_call({}),'text':'not json'};attempts=[]
        with self.assertRaises(specs.CompanyModelSpecError)as raised:
            cli._validated_spec_with_repair(model=Model(),state=state,mission={},original_call=initial,
                repair_config={'max_attempts':5},decided_by='automation:test',repair_attempts=attempts)
        self.assertEqual(raised.exception.code,PERIODICITY_CODE)
        self.assertEqual(len(calls),1);self.assertEqual(attempts[0]['cost_micros'],17)


    def test_producer_prevents_fields_with_new_marker_only_old_template_unchanged(self):
        state,_,_=identities();current=specs.build_prompt(state)
        self.assertIn('periodicity is exactly quarterly or annual',current)
        self.assertIn('acquisitions cash line cannot use zero_one_off',current)
        self.assertIn('from one coherent disclosed revenue group',current)
        legacy=copy.deepcopy(state);legacy['numeric_context'].pop('revenue_group_candidates')
        self.assertNotIn('FIELD REQUIREMENTS:',specs.build_prompt(legacy))
        # Shared assessment-template hash remains its exact historical identity.
        self.assertEqual(specs.assessment_compatibility_contract()['current_task_hash'],specs.TASK_HASH)

    def test_real_full_dag_validation_runs_after_field_repair_and_never_gets_waived(self):
        from tests import test_model_spec_signed_expense_repair as financial
        for ties in [True,False]:
            state,body,inputs=financial.case()
            if ties:body=financial.fixed(body)
            bad=copy.deepcopy(body);bad['operating_metrics'][0]['periodicity']='quarterly_or_annual'
            calls=[];validations=[];attempts=[]
            class Model:
                scheduler_db=None
                def call(self,**kw):calls.append(kw);return {**cash_fixture.root_call(body),'cost_micros':82}
            run=lambda:cli._validated_spec_with_repair(model=Model(),state=state,mission={},original_call=cash_fixture.root_call(bad),
                repair_config={'max_attempts':5},decided_by='automation:test',repair_attempts=attempts,
                financial_validator=financial.callback(inputs,validations))
            if ties:run()
            else:
                with self.assertRaises(specs.CompanyModelSpecError):run()
            self.assertEqual(len(calls),1);self.assertEqual(attempts[0]['cost_micros'],82)
            self.assertEqual(len(validations),1)


    def test_unrelated_json_type_change_is_not_equal_parent_despite_python_equality(self):
        state,bad,fixed=periodicity();scope=error_for(state,bad).proof
        fixed['operating_metrics'][0]['disclosed']=1
        self.assertEqual(fixed['operating_metrics'][0]['disclosed'],bad['operating_metrics'][0]['disclosed'])
        with self.assertRaises(specs.CompanyModelSpecError):
            assert_only_typed_fields_changed(bad,fixed,scope,state=state)
        state,bad,fixed=identities();scope=error_for(state,bad).proof
        scope['paths'][0][1]=False
        with self.assertRaises(specs.CompanyModelSpecError):validate_parent(bad,scope,state=state)
