"""Exact multi-error scope and real Scheduler admission, without network."""
import copy
import hashlib
import json
import sqlite3
import tempfile
import unittest
from dataclasses import replace
from contextlib import closing
from pathlib import Path
from unittest.mock import patch
from dalton_core import company_model_cli as cli,company_model_spec as specs
from dalton_core import model_spec_complete_diagnostics as diag
from dalton_core.cockpit_model import CockpitModelError
from dalton_core.scheduler import Scheduler
from dalton_core.store import canonical_json,content_hash
from tests.test_model_spec_cash_flow_base_repair import proposal,unavailable,root_call
from tests import test_cockpit_model_fallback as runtime


def case(company='company:ticker:cedar'):
    state,bad=proposal();state['company_ref']=company
    bad['financial_statement_structure']['schema_version']='0.6'
    for line in bad['financial_statement_structure']['lines']:
        line['annual_forecast_method']='annual_growth_from_filed' if line['role']=='diluted_weighted_average_shares' else None
        if line['role']in ('diluted_weighted_average_shares','diluted_eps'):
            line['annual_semantics']='not_applicable'
    fixed=unavailable(bad)
    for line in fixed['financial_statement_structure']['lines']:
        if line['role']=='diluted_weighted_average_shares':line['annual_semantics']='direct_annual'
        if line['role']=='diluted_eps':line['annual_semantics']='annual_ratio'
    return state,bad,fixed


class CompleteDiagnosticsTests(unittest.TestCase):
    def test_two_companies_multi_error_no_original_edit_and_one_repair_full_validator(self):
        for company in ('company:ticker:cedar','company:ticker:maple'):
            state,bad,fixed=case(company);raw=json.dumps(bad);before=hashlib.sha256(raw.encode()).hexdigest()
            class Model:
                scheduler_db=None
                def __init__(self):self.calls=[]
                def call(self,**kwargs):self.calls.append(kwargs);return {**root_call(fixed),'cost_micros':19}
            model=Model();checked=[]
            spec,attempts,_=cli._validated_spec_with_repair(model=model,state=state,mission={},
                original_call=root_call(bad),repair_config={'max_attempts':1},decided_by='automation:test',
                financial_validator=lambda v:checked.append(v['company_ref']))
            self.assertEqual(checked,[company]);self.assertEqual(len(model.calls),1)
            proof=model.calls[0]['_structured_output_repair']['diagnostic_bundle']
            self.assertEqual(proof['finding_count'],3);self.assertEqual(len(proof['findings']),3)
            self.assertIn('3.',model.calls[0]['prompt']);self.assertEqual(attempts[0]['cost_micros'],19)
            self.assertEqual(spec['company_ref'],company)
            self.assertEqual(hashlib.sha256(json.dumps(bad).encode()).hexdigest(),before)

    def test_whole_body_guard_refuses_each_unrelated_field_and_reorder(self):
        _,bad,fixed=case();proof=diag.diagnose(bad)
        for change in ('assessment','concept','formula','evidence','capex','fcf','order','horizon','extra','delete'):
            value=copy.deepcopy(fixed)
            if change=='assessment':value['assessment']='Other judgement'
            if change=='concept':value['financial_statement_structure']['lines'][0]['concept']='foreign:Revenue'
            if change=='formula':value['financial_statement_structure']['formulas'][0]['terms'][0]['coefficient']='2'
            if change=='evidence':value['financial_statement_structure']['formulas'][0]['evidence_refs']=['foreign']
            if change=='capex':value['cash_flow_companion']['lines'][1]['concept']=None
            if change=='fcf':value['cash_flow_companion']['formula']['terms'][1]['coefficient']='1'
            if change=='order':value['financial_statement_structure']['lines'].reverse()
            if change=='horizon':value['horizon']['forecast_quarters']=12
            if change=='extra':value['cash_flow_companion']['lines'][0]['invented']=1
            if change=='delete':del value['cash_flow_companion']['lines'][0]['because']
            with self.subTest(change=change),self.assertRaisesRegex(specs.CompanyModelSpecError,'outside its exact'):
                diag.assert_only_allowed_changed(bad,value,proof)

    def test_shape_refs_and_cycles_block_dependent_diagnostics(self):
        for change in ('missing','duplicate','formula_ref','cycle','eps_dependency'):
            _,bad,_=case()
            if change=='missing':del bad['assessment']
            if change=='duplicate':bad['financial_statement_structure']['lines'][1]['ref']=bad['financial_statement_structure']['lines'][0]['ref']
            if change=='formula_ref':bad['financial_statement_structure']['formulas'][0]['terms'][0]['line_ref']='foreign'
            if change=='cycle':
                f=bad['financial_statement_structure']['formulas'][0];f['terms'][0]['line_ref']=f['output_ref']
            if change=='eps_dependency':
                f=next(f for f in bad['financial_statement_structure']['formulas']if f['operator']=='divide');f['denominator_ref']='revenue'
            report=diag.diagnose(bad)
            if change=='eps_dependency':self.assertNotIn('eps_annual_semantics',[f['rule']for f in report['findings']])
            else:self.assertEqual(report['status'],'blocked');self.assertEqual(report['findings'],[])

    def test_shares_unit_period_or_kind_error_not_misclassified_as_annual(self):
        for field,value in (('unit','usd'),('period_kind','instant'),('kind','derived')):
            _,bad,_=case();line=next(v for v in bad['financial_statement_structure']['lines']if v['role']=='diluted_weighted_average_shares');line[field]=value
            self.assertNotIn('shares_annual_semantics',[v['rule']for v in diag.diagnose(bad)['findings']])

    def test_overflow_does_not_offer_partial_repair(self):
        _,bad,_=case();s=bad['financial_statement_structure'];template=next(v for v in s['lines']if v['role']=='diluted_eps');f=next(v for v in s['formulas']if v['output_ref']==template['ref'])
        for i in range(12):
            line={**template,'ref':f'eps-extra-{i}'};s['lines'].append(line);s['formulas'].append({**copy.deepcopy(f),'output_ref':line['ref']})
        report=diag.diagnose(bad);self.assertEqual(report['status'],'overflow');self.assertEqual(report['findings'],[])
        self.assertEqual(len(report['overflow_findings_hash']),64)
        class Model:
            scheduler_db=None
            def call(self,**kwargs):raise AssertionError('overflow cannot buy partial repair')
        with self.assertRaisesRegex(specs.CompanyModelSpecError,'fixed finding limit'):
            cli._validated_spec_with_repair(model=Model(),state=case()[0],mission={},original_call=root_call(bad),
                repair_config={'max_attempts':1},decided_by='automation:test')

    def test_dynamic_derived_cash_contract_does_not_downgrade_legal_base(self):
        _,bad,fixed=case()
        import sys,types
        module=types.ModuleType('dalton_core.cash_flow_derived_base');module.CONTRACT_REF='cash-flow-derived-income-base:0.1'
        module.derived_base_is_forecastable=lambda s,ref:ref=='net'
        with patch.dict(sys.modules,{'dalton_core.cash_flow_derived_base':module}):
            proof=diag.diagnose(bad)
            self.assertEqual([v['rule']for v in proof['findings']],['shares_annual_semantics','eps_annual_semantics'])
            fixed['cash_flow_companion']=copy.deepcopy(bad['cash_flow_companion'])
            diag.assert_only_allowed_changed(bad,fixed,proof)
        with self.assertRaises(specs.CompanyModelSpecError):diag.validate_parent(bad,proof)

    def test_full_financial_error_still_refuses_without_second_call(self):
        state,bad,fixed=case()
        class Model:
            scheduler_db=None
            calls=0
            def call(self,**kwargs):self.calls+=1;return {**root_call(fixed),'cost_micros':19}
        model=Model();attempts=[]
        def full(v):raise specs.CompanyModelSpecError('formula does not tie to filed history')
        with self.assertRaisesRegex(specs.CompanyModelSpecError,'does not tie'):
            cli._validated_spec_with_repair(model=model,state=state,mission={},original_call=root_call(bad),
                repair_config={'max_attempts':1},decided_by='automation:test',financial_validator=full,repair_attempts=attempts)
        self.assertEqual(model.calls,1);self.assertEqual(attempts[0]['cost_micros'],19)

    def test_existing_slot_success_failed_inflight_unknown_and_tamper_is_not_new_slot(self):
        for status in ('succeeded','failed','active','unknown','tampered'):
            state,bad,_=case();root=root_call(bad);proof={k:root[k]for k in cli._REPAIR_AUTHORITY_KEYS}
            with tempfile.TemporaryDirectory()as d:
                p=Path(d)/'scheduler.sqlite';c=sqlite3.connect(p)
                c.execute('CREATE TABLE scheduler_work_orders(work_order_id TEXT,work_order_json TEXT,work_order_hash TEXT)')
                w={'metadata':{'purpose':'model_spec','structured_output_repair':{'root_original':proof,'repair_contract_ref':'contract:company-model-spec-structured-output-repair:0.12'}},'status':status}
                wire=canonical_json(w);c.execute('INSERT INTO scheduler_work_orders VALUES(?,?,?)',('work:cockpit-model_spec-old',wire,content_hash(w)if status!='tampered'else'0'*64));c.commit();c.close();before=p.read_bytes()
                class Model:
                    scheduler_db=p
                    def call(self,**kwargs):raise AssertionError('old root repair slot cannot be bought again')
                with self.assertRaises(specs.CompanyModelSpecError):
                    cli._validated_spec_with_repair(model=Model(),state=state,mission={},original_call=root,
                        repair_config={'max_attempts':1},decided_by='automation:test')
                self.assertEqual(p.read_bytes(),before)


class BundleAdmissionTests(unittest.TestCase):
    def run_case(self, mutate=None):
        fixture=runtime.CockpitChainTests('test_model_spec_repair_is_a_second_budgeted_scheduler_work');fixture.setUp();self.addCleanup(fixture.doCleanups)
        state,bad,fixed=case()
        class Adapter(runtime.ChainAdapter):
            def __init__(self):super().__init__({});self.outputs=[bad,fixed]
            def execute(self,work,route,profile):
                inv,env=super().execute(work,route,profile)
                return inv,replace(env,outputs={'text':json.dumps(self.outputs.pop(0))})
        adapter=Adapter();model=fixture._model(adapter,policy_version_ref=fixture.pinned_policy,model_spec_output_tokens=6000)
        root=model.call(purpose='model_spec',request_id=cli.model_spec_request_id(state['state_hash'],repair_config={'max_attempts':1}),
            prompt=specs.build_prompt(state),mission=fixture.mission,
            _model_spec_request_identity=cli.model_spec_request_identity(state['state_hash'],repair_config={'max_attempts':1}))
        real=model.call
        def intercept(**kw):
            if mutate and '_structured_output_repair'in kw:
                binding=copy.deepcopy(kw['_structured_output_repair']);mutate(binding)
                error=specs.CompanyModelSpecError(binding['validation_error']['message'],code=binding['validation_error']['code'],proof=binding.get('diagnostic_bundle'))
                kw['prompt']=cli._repair_prompt(root['text'],error);binding['repair_prompt_sha256']=hashlib.sha256(kw['prompt'].encode()).hexdigest()
                kw['_structured_output_repair']=binding;kw['request_id']='model-spec-repair:'+content_hash(binding)[:32]
            return real(**kw)
        with patch.object(model,'call',side_effect=intercept):
            if mutate:
                with self.assertRaises(CockpitModelError):cli._validated_spec_with_repair(model=model,state=state,mission=fixture.mission,original_call=root,repair_config={'max_attempts':1},decided_by='automation:test')
                self.assertEqual(len(adapter.served),1)
            else:
                spec,repairs,call=cli._validated_spec_with_repair(model=model,state=state,mission=fixture.mission,original_call=root,repair_config={'max_attempts':1},decided_by='automation:test')
                self.assertEqual(len(adapter.served),2);self.assertEqual(len(repairs),1)
                with Scheduler(fixture.root/'scheduler.sqlite')as scheduler:
                    old=scheduler.work_order_authority(root['work_order_ref']);new=scheduler.work_order_authority(call['work_order_ref'])
                    self.assertEqual(old['work_order']['budget'],new['work_order']['budget'])
                cli._validated_spec_with_repair(model=model,state=state,mission=fixture.mission,original_call=root,repair_config={'max_attempts':0},decided_by='automation:test')
                self.assertEqual(len(adapter.served),2)
    def test_real_admission_single_slot_and_free_replay(self):self.run_case()
    def test_same_binding_paid_unknown_or_inflight_cannot_buy_with_new_budget(self):
        for status in ('paid_invalid', 'timeout_unknown', 'inflight'):
            with self.subTest(status=status):
                fixture=runtime.CockpitChainTests('test_model_spec_repair_is_a_second_budgeted_scheduler_work')
                fixture.setUp();self.addCleanup(fixture.doCleanups)
                state,bad,_=case()
                class Adapter(runtime.ChainAdapter):
                    def __init__(self):super().__init__({});self.number=0
                    def execute(self,work,route,profile):
                        self.number+=1;inv,env=super().execute(work,route,profile)
                        if self.number==2 and status=='inflight':raise KeyboardInterrupt('isolated interrupted paid Work fixture')
                        if self.number==1 or status=='paid_invalid':
                            return inv,replace(env,outputs={'text':json.dumps(bad)})
                        return inv,replace(env,status='failed',outputs={},error={'code':'TIMEOUT','message':'provider response is unknown'})
                adapter=Adapter();model=fixture._model(adapter,policy_version_ref=fixture.pinned_policy,model_spec_output_tokens=6000)
                root=model.call(purpose='model_spec',request_id=cli.model_spec_request_id(state['state_hash'],repair_config={'max_attempts':1}),
                    prompt=specs.build_prompt(state),mission=fixture.mission,
                    _model_spec_request_identity=cli.model_spec_request_identity(state['state_hash'],repair_config={'max_attempts':1}))
                args=dict(state=state,mission=fixture.mission,original_call=root,repair_config={'max_attempts':1},decided_by='automation:test')
                with self.assertRaises((specs.CompanyModelSpecError,CockpitModelError,KeyboardInterrupt)):
                    cli._validated_spec_with_repair(model=model,**args)
                self.assertEqual(len(adapter.served),2)
                db=Path(model.scheduler_db)
                with closing(sqlite3.connect(db))as c:
                    before=c.execute('SELECT work_order_id,work_order_json,work_order_hash FROM scheduler_work_orders ORDER BY work_order_id').fetchall()
                changed=fixture._model(adapter,policy_version_ref=fixture.pinned_policy,model_spec_output_tokens=7000)
                with patch.object(changed,'call',wraps=changed.call)as called:
                    with self.assertRaises(specs.CompanyModelSpecError):cli._validated_spec_with_repair(model=changed,**args)
                    called.assert_not_called()
                self.assertEqual(len(adapter.served),2)
                with closing(sqlite3.connect(db))as c:
                    self.assertEqual(before,c.execute('SELECT work_order_id,work_order_json,work_order_hash FROM scheduler_work_orders ORDER BY work_order_id').fetchall())

    def test_capacity_receipt_pk_and_event_ref_cannot_rebind_another_envelope(self):
        original_row = diag._row
        def altered(connection, query, params):
            row = original_row(connection, query, params)
            if row is not None and 'scheduler_attempt_events WHERE event_id' in query:
                row=dict(row);row['result_envelope_id']='result:foreign'
                wire={"schema_version":"0.1","wire_version":"0.2","id":row['event_id'],
                    "created_at":row['created_at'],"work_order_ref":row['work_order_id'],
                    "attempt_number":row['attempt_number'],"state":row['state'],
                    "lease_ref":row['lease_revision_id'],"result_envelope_ref":row['result_envelope_id'],
                    "result_envelope_hash":row['result_envelope_hash'],"reason":row['reason'],
                    "not_before":row['not_before'],"prior_event_ref":row['prior_event_id']}
                row['content_hash']=content_hash(wire)
            if 'scheduler_result_envelopes WHERE result_envelope_id' in query and params==('result:foreign',):
                cursor=connection.execute('SELECT * FROM scheduler_result_envelopes WHERE outcome=? ORDER BY attempt_number DESC LIMIT 1',('retryable',))
                row=dict(zip((x[0]for x in cursor.description),cursor.fetchone()))
                row['result_envelope_id']='result:foreign'
                row['content_hash']=content_hash({k:row[k]for k in ('result_envelope_id','work_order_id','attempt_number','result_envelope_hash','outcome','created_at')})
            return row
        fixture=runtime.CockpitChainTests('test_model_spec_repair_survives_versioned_capacity_recovery')
        fixture.setUp();self.addCleanup(fixture.doCleanups)
        with patch.object(diag,'_row',side_effect=altered),self.assertRaises(specs.CompanyModelSpecError):
            fixture.test_model_spec_repair_survives_versioned_capacity_recovery()

    def test_v012_frozen_contract_prompt_and_primary_identity_are_unchanged(self):
        fixture=json.loads((Path(__file__).parent/'fixtures/model_spec_repair_contract_v012.json').read_bytes())
        error=specs.CompanyModelSpecError(fixture['error']['message'],code=fixture['error']['code'])
        self.assertEqual(fixture['repair_contract']['ref'],'contract:company-model-spec-structured-output-repair:0.12')
        self.assertEqual(hashlib.sha256(cli._repair_prompt(fixture['parent_text'],error,
            contract=fixture['repair_contract']).encode()).hexdigest(),fixture['prompt_sha256'])
        self.assertEqual(specs.TASK_HASH,fixture['task_hash'])
        self.assertEqual(hashlib.sha256(json.dumps(specs.OUTPUT_SCHEMA,ensure_ascii=False,sort_keys=True).encode()).hexdigest(),fixture['output_schema_hash'])

    def test_forged_rehashed_paths_or_parent_rejected_before_provider(self):
        for field in ('parent','path','choice'):
            def mutate(b):
                p=b['diagnostic_bundle']
                if field=='parent':p['parent_body_hash']='e'*64
                if field=='path':p['findings'][0]['paths'][0][-1]='unit'
                if field=='choice':p['findings'][0]['choices']=[['arbitrary']]
            with self.subTest(field=field):self.run_case(mutate)
