"""One original slot for joint annotations; missing execution gets real material work."""
import copy
import hashlib
import json
import sqlite3
import unittest
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from dalton_core import company_model_cli as cli, company_model_spec as specs
from dalton_core import model_spec_joint_field_diagnostics as joint
from dalton_core.cockpit_model import CockpitModelError
from dalton_core.scheduler import Scheduler
from dalton_core.store import content_hash, canonical_json
from tests import test_model_spec_complete_diagnostics as settings
from tests import test_model_spec_cash_flow_base_repair as calls
from tests import test_cockpit_model_fallback as runtime


def case(company='company:ticker:cedar'):
    state, bad, fixed = settings.case(company)
    # This test's cash method is already explicitly unavailable; only the
    # explanation, optional annotation and two annual settings are defective.
    bad['cash_flow_companion'] = copy.deepcopy(fixed['cash_flow_companion'])
    bad['expense_lines'][0].update(cost_driver_slot='fixed_cost', cost_driver_unbound_reason='')
    bad['expense_lines'][0].pop('because')
    fixed['expense_lines'][0]['cost_driver_slot'] = 'fixed_cost'
    state['state_hash'] = content_hash({k:v for k,v in state.items() if k!='state_hash'})
    return state, bad, fixed


def source_case(company='company:ticker:cedar'):
    state, _, body = case(company)
    body['revenue_drivers'] = [{'ref':'quantity-price', 'label':'Unobserved quantity and realized price',
        'kind':'external', 'basis_concept':None, 'unit':'index',
        'because':'No admitted quantity or realized price source exists in this input.'}]
    body['driver_formulas'] = []
    for line in body['expense_lines']:
        line['driver_ref'] = None
    body['expense_lines'][0].pop('because')
    body['operating_metrics'] = [{'ref':'units-sold','label':'Units sold','unit':'units',
        'periodicity':'quarterly','disclosed':False,'because':'No original unit-volume disclosure is admitted.'},
        {'ref':'selling-price','label':'Average selling price','unit':'usd','periodicity':'quarterly',
         'disclosed':False,'because':'No original realized price disclosure is admitted.'}]
    state['numeric_context'] = {'cells':[{'status':'filed','period_shape':'quarter',
        'period_start':'2026-01-01','period_end':'2026-03-31'}],
        'content_hash':'1'*64,'policy':{},'included_cells':0,'omitted_by_series_limit':0,
        'omitted_by_total_limit':0,'omitted_by_prompt_limit':0,'prompt_byte_limit':128000,'prompt_bytes':10000}
    state['state_hash'] = content_hash({k:v for k,v in state.items() if k!='state_hash'})
    return state, body


class JointFieldsTests(unittest.TestCase):
    def test_two_companies_all_independent_fields_one_call_full_validation(self):
        for company in ('company:ticker:cedar', 'company:ticker:maple'):
            state,bad,fixed=case(company); before=copy.deepcopy(bad); seen=[];checked=[]
            class Model:
                scheduler_db=None
                def call(self,**kw):seen.append(kw);return {**calls.root_call(fixed),'cost_micros':19}
            spec, repairs, _ = cli._validated_spec_with_repair(model=Model(),state=state,mission={},
                original_call=calls.root_call(bad),repair_config={'max_attempts':1},decided_by='automation:test',
                financial_validator=lambda v:checked.append(v))
            self.assertEqual(len(seen),1);self.assertEqual(len(checked),1);self.assertEqual(spec['company_ref'],company)
            self.assertEqual(repairs[0]['cost_micros'],19);self.assertEqual(bad,before)
            proof=seen[0]['_structured_output_repair']['diagnostic_bundle']
            self.assertEqual([f['rule'] for f in proof['findings']],
                ['required_expense_explanation','empty_optional_bound_slot_reason','shares_annual_semantics','eps_annual_semantics'])
            self.assertNotIn('driver_formulas', [p[0] for f in proof['findings'] for p in f['paths']])

    def test_source_missing_driver_is_not_an_explanation_only_paid_repair(self):
        state,bad=source_case();before=copy.deepcopy(bad)
        class Model:
            scheduler_db=None
            def call(self,**kw):raise AssertionError('missing driver source cannot buy annotation repair')
        with self.assertRaises(specs.CompanyModelSpecError) as caught:
            cli._validated_spec_with_repair(model=Model(),state=state,mission={},original_call=calls.root_call(bad),
                repair_config={'max_attempts':1},decided_by='automation:test')
        error=caught.exception;self.assertEqual(error.code,joint.SOURCE_CODE)
        self.assertIn('units-sold (units)',str(error));self.assertIn('2026-03-31',str(error)[:500])
        self.assertIn('quantity-price',str(error));self.assertEqual(bad,before)

    def test_closed_shape_dependency_invalid_costslot_and_unrelated_changes_refuse(self):
        state,bad,fixed=case();proof=joint.from_state(bad,state)
        for field in ('assessment','driver','period','slot','extra','formula','reorder','reason','partial'):
            value=copy.deepcopy(fixed)
            if field=='assessment':value['assessment']='Different economics'
            if field=='driver':value['revenue_drivers'][0]['basis_concept']='foreign:Concept'
            if field=='period':value['operating_metrics'][0]['periodicity']='annual'
            if field=='slot':value['expense_lines'][0]['cost_driver_slot']=None
            if field=='extra':value['expense_lines'][0]['invented']=1
            if field=='formula':value['financial_statement_structure']['formulas'][0]['terms'][0]['coefficient']='2'
            if field=='reorder':value['expense_lines'].reverse()
            if field=='reason':value['expense_lines'][0]['because']=' '
            if field=='partial':value['financial_statement_structure']['lines'][7]['annual_semantics']='not_applicable'
            with self.subTest(field=field),self.assertRaises(specs.CompanyModelSpecError):
                joint.assert_only_allowed_changed(bad,value,proof)
        for field in ('slot','shape','ref','reason_type'):
            value=copy.deepcopy(bad)
            if field=='slot':value['expense_lines'][0]['cost_driver_slot']='foreign-slot'
            if field=='shape':value['expense_lines'][0]['extra']=1
            if field=='ref':value['financial_statement_structure']['lines'][1]['ref']=value['financial_statement_structure']['lines'][0]['ref']
            if field=='reason_type':value['expense_lines'][0]['because']=None
            self.assertEqual(joint.from_state(value,state)['status'],'blocked')

    def test_no_second_paid_call_after_full_financial_refusal(self):
        state,bad,fixed=case();seen=[];attempts=[]
        class Model:
            scheduler_db=None
            def call(self,**kw):seen.append(kw);return {**calls.root_call(fixed),'cost_micros':19}
        def full(spec):raise specs.CompanyModelSpecError('formula does not tie to filed history')
        with self.assertRaisesRegex(specs.CompanyModelSpecError,'does not tie'):
            cli._validated_spec_with_repair(model=Model(),state=state,mission={},original_call=calls.root_call(bad),
                repair_config={'max_attempts':1},decided_by='automation:test',financial_validator=full,repair_attempts=attempts)
        self.assertEqual(len(seen),1);self.assertEqual(attempts[0]['cost_micros'],19)


class JointAdmissionTests(unittest.TestCase):
    def fixture(self, *, outputs=None, state_body=None, mission=None):
        f=runtime.CockpitChainTests('test_model_spec_repair_is_a_second_budgeted_scheduler_work')
        f.setUp();self.addCleanup(f.doCleanups)
        state,bad,fixed=case() if state_body is None else (*state_body,None)
        if mission is not None:f.mission=mission
        class Adapter(runtime.ChainAdapter):
            def __init__(inner):super().__init__({});inner.outputs=list(outputs or [bad,fixed])
            def execute(inner,work,route,profile):
                inv,env=super().execute(work,route,profile);value=inner.outputs.pop(0)
                if isinstance(value,BaseException):raise value
                if value=='unknown':return inv,replace(env,status='failed',outputs={},error={'code':'TIMEOUT','message':'sent state unknown'})
                return inv,replace(env,outputs={'text':json.dumps(value)})
        adapter=Adapter();model=f._model(adapter,policy_version_ref=f.pinned_policy,model_spec_output_tokens=6000)
        root=model.call(purpose='model_spec',request_id=cli.model_spec_request_id(state['state_hash'],repair_config={'max_attempts':1}),
            prompt=specs.build_prompt(state),mission=f.mission,_model_spec_request_identity=cli.model_spec_request_identity(state['state_hash'],repair_config={'max_attempts':1}))
        return f,state,bad,fixed,model,adapter,root

    def test_actual_scheduler_root_repair_restart_same_original_slot_and_fee(self):
        from datetime import timedelta
        f,state,bad,fixed,model,adapter,root=self.fixture();checked=[]
        args=dict(state=state,mission=f.mission,original_call=root,repair_config={'max_attempts':1},decided_by='automation:test')
        spec,repairs,paid=cli._validated_spec_with_repair(model=model,financial_validator=lambda v:checked.append(v),**args)
        self.assertEqual(len(adapter.served),2);self.assertEqual(len(checked),1)
        with Scheduler(model.scheduler_db) as owner:
            before=owner.connection.execute('SELECT work_order_id,work_order_json,work_order_hash FROM scheduler_work_orders ORDER BY work_order_id').fetchall()
        model.clock=lambda:runtime.NOW+timedelta(days=1)
        args['repair_config']={'max_attempts':0}
        _,again,accepted=cli._validated_spec_with_repair(model=model,**args)
        self.assertEqual(accepted['work_order_ref'],paid['work_order_ref']);self.assertEqual(len(adapter.served),2)
        self.assertTrue(again[0]['replayed']);self.assertEqual(again[0]['cost_micros'],0)
        with Scheduler(model.scheduler_db) as owner:
            self.assertEqual(before,owner.connection.execute('SELECT work_order_id,work_order_json,work_order_hash FROM scheduler_work_orders ORDER BY work_order_id').fetchall())

    def test_rehashed_foreign_proof_cannot_admit(self):
        f,state,bad,_,model,adapter,root=self.fixture();real=model.call
        def tamper(**kw):
            binding=copy.deepcopy(kw['_structured_output_repair']);binding['diagnostic_bundle']['findings'][0]['paths'][0][-1]='basis_concept'
            error=specs.CompanyModelSpecError(binding['validation_error']['message'],code=joint.ERROR_CODE,proof=binding['diagnostic_bundle'])
            # Do not use the production prompt helper (it rightly refuses
            # forged proof). The attacker can rehash a public forged string.
            kw['prompt']='forged';binding['repair_prompt_sha256']=hashlib.sha256(kw['prompt'].encode()).hexdigest()
            kw['_structured_output_repair']=binding;kw['request_id']='model-spec-repair:'+content_hash(binding)[:32]
            return real(**kw)
        with patch.object(model,'call',side_effect=tamper),self.assertRaises(CockpitModelError):
            cli._validated_spec_with_repair(model=model,state=state,mission=f.mission,original_call=root,
                repair_config={'max_attempts':1},decided_by='automation:test')
        self.assertEqual(len(adapter.served),1)

    def test_paid_failed_unknown_or_inflight_slot_cannot_buy_with_new_budget(self):
        state,bad,_=case()
        for output in (bad,'unknown',KeyboardInterrupt('isolated interrupted attempt')):
            f,state,bad,_,model,adapter,root=self.fixture(outputs=[bad,output])
            args=dict(state=state,mission=f.mission,original_call=root,repair_config={'max_attempts':1},decided_by='automation:test')
            with self.assertRaises((specs.CompanyModelSpecError,CockpitModelError,KeyboardInterrupt)):
                cli._validated_spec_with_repair(model=model,**args)
            self.assertEqual(len(adapter.served),2)
            model.config['purpose_call_budgets']['model_spec']['max_output_tokens']=7000
            with patch.object(model,'call',wraps=model.call) as called,self.assertRaises(specs.CompanyModelSpecError):
                cli._validated_spec_with_repair(model=model,**args)
            called.assert_not_called();self.assertEqual(len(adapter.served),2)


class DeclaredDriverMaterialTests(unittest.TestCase):
    def setup_source(self):
        from tests import test_company_model_cli as governed_fixture
        from tests import test_material_acquisition as materials
        g=governed_fixture.ChooseCompanyTests('runTest');g.setUp();self.addCleanup(g.doCleanups)
        g.mission=g.missions._mission_record(g.mission['id'])
        m=materials.Fixture('runTest');m.setUp();self.addCleanup(m.doCleanups)
        state,bad=source_case(governed_fixture.ACN)
        h=JointAdmissionTests('runTest');h.addCleanup=self.addCleanup
        f,_,_,_,model,adapter,root=h.fixture(state_body=(state,bad),outputs=[bad],mission=g.mission)
        scheduler=Scheduler(model.scheduler_db);self.addCleanup(scheduler.close)
        return g,m,state,bad,model,adapter,root

    def test_success_original_opens_regular_request_once_and_held_source_is_really_served(self):
        from dalton_core import material_requests as mr, material_acquisition as ma
        from tests import test_material_acquisition as materials
        g,m,state,bad,model,adapter,root=self.setup_source()
        def opened():return joint.open_source_request(missions=g.missions,model=model,state=state,
            mission=g.mission,call=root,state_dir=m.state)
        before=copy.deepcopy(root);requests=opened();duplicates=opened()
        self.assertEqual(len(requests),2)
        first,again=requests[0],duplicates[0]
        self.assertEqual(first['id'],again['id']);self.assertEqual(first['write_status'],'fresh')
        self.assertEqual(again['write_status'],'duplicate');self.assertEqual(root,before)
        self.assertEqual(len(adapter.served),1);self.assertFalse(first['anchor']['financial_evidence'])
        m.corpus.add('sec_10q','Held quarterly operating disclosure',
            'Units sold and Average selling price: for January 1 to March 31, 2026, '
            'units sold were 40 tons and average selling price was 12 USD per ton. '
            'These are original operating disclosures, not an inferred consolidated revenue split.',
            date='2026-04-30',company=state['company_ref'])
        paid=SimpleNamespace(costed=True,applies=lambda _:True,
            estimate_calls=lambda _:self.fail('held original must precede paid acquisition'))
        result=ma.serve(m.state,first['id'],now=materials.NOW,
            rungs={'indexed_corpus':ma.IndexedCorpusRung(),'sec_edgar':paid})
        self.assertEqual(result['outcome'],'found');self.assertEqual(result['rungs'][0]['source'],'indexed_corpus')
        self.assertEqual(mr.request(m.state,first['id'])['attempt_count'],1)
        cached=ma.serve(m.state,first['id'],now=materials.NOW,rungs={'indexed_corpus':ma.IndexedCorpusRung(),'sec_edgar':paid})
        self.assertEqual(cached['outcome'],'cached');self.assertEqual(len(adapter.served),1)
        # Availability is not an accepted financial model or source-derived formula.
        self.assertEqual(g.missions.company_model_specs(state['company_ref']),[])

    def test_current_permission_source_state_raw_or_unsettled_charge_cannot_open(self):
        from dalton_core import material_requests as mr
        for kind in ('permission','source','raw','unsettled'):
            g,m,state,bad,model,adapter,root=self.setup_source()
            if kind=='permission':g.mission=copy.deepcopy(g.mission);g.mission['autonomy']['may_write']=[]
            if kind=='source':state['concepts'].append('foreign:Line')
            if kind=='raw':root=copy.deepcopy(root);root['text']=root['text']+' '
            if kind=='unsettled':
                with sqlite3.connect(model.config['budget_db']) as c:
                    for row in c.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='thesis_impact_day_settlements'").fetchall():c.execute('DROP TRIGGER '+row[0])
                    c.execute('DELETE FROM thesis_impact_day_settlements')
            with self.subTest(kind=kind),self.assertRaises(ValueError):
                joint.open_source_request(missions=g.missions,model=model,state=state,mission=g.mission,call=root,state_dir=m.state)
            self.assertEqual(mr.open_requests(m.state),[]);self.assertEqual(len(adapter.served),1)

    def test_normal_cli_refusal_registers_source_task_and_checked_planner_failure(self):
        from dalton_core.lane_failure_ledger import LaneFailureLedger
        from dalton_core.model_execution_failure_feedback import last_failures
        from datetime import datetime,timezone
        g,m,state,bad,model,adapter,root=self.setup_source()
        config=g.state_dir/'model.json';config.write_text(json.dumps(model.config))
        with patch.object(cli,'CockpitModel',return_value=model),patch.object(cli,'choose_company',return_value=(state['company_ref'],state)),patch('dalton_core.statement_original_revenue_context.prepare_model_revenue_inputs',return_value={}),patch('dalton_core.sec_filing_units.prepare_model_unit_inputs',return_value={'unresolved':[],'refreshed':False}):
            summary=cli.run_model_spec(state_dir=g.state_dir,model_config_path=config,scheduler_db=model.scheduler_db,
                summary_dir=g.state_dir/'source-run',company_ref=state['company_ref'])
        self.assertEqual(summary['spec_status'],'refused');self.assertEqual(summary['repair_attempts'],[])
        self.assertIn('declared_driver_material_requests',summary);self.assertEqual(len(adapter.served),1)
        self.assertEqual(summary['declared_driver_material_requests'][0]['status'],'open')
        ledger=LaneFailureLedger(g.state_dir/'lane-failure-ledger.sqlite');self.addCleanup(ledger.close)
        ledger.append_event(lane='mission_model_spec',item_key=state['company_ref']+'|exact-input',event='terminal',
            failure_class='content_refused',reason=summary['failure_reason'],rule='lane_content_refusal',status='refused',at=datetime.now(timezone.utc))
        feedback=last_failures(g.store.connection,state_dir=g.state_dir,company_ref=state['company_ref'])
        self.assertIn('units-sold',feedback['items'][0]['reason']);self.assertIn('2026-03-31',feedback['items'][0]['reason'])
        self.assertFalse(feedback['items'][0]['retry_authority'])


class JointNormalPersistenceTests(unittest.TestCase):
    def test_normal_cli_stores_complete_repaired_spec_and_restart_never_rebuys(self):
        from tests.test_company_model_cli import ChooseCompanyTests,ACN
        from tests import test_model_spec_signed_expense_repair as financial
        from dalton_core.coverage_mission import CoverageMissionAuthority,company_model_spec_id
        g=ChooseCompanyTests('runTest');g.setUp();self.addCleanup(g.doCleanups)
        mission=g.missions._mission_record(g.mission['id'])
        state,body,inputs=financial.case();body=financial.fixed(body)
        state['company_ref']=inputs['company_ref']=ACN
        state['numeric_context']={'content_hash':'1'*64,'policy':{},'included_cells':0,
            'omitted_by_series_limit':0,'omitted_by_total_limit':0,'omitted_by_prompt_limit':0,
            'prompt_byte_limit':128000,'prompt_bytes':12000}
        state['state_hash']=content_hash({k:v for k,v in state.items() if k!='state_hash'})
        body['financial_statement_structure']['schema_version']='0.6'
        for line in body['financial_statement_structure']['lines']:
            line['annual_forecast_method']='annual_growth_from_filed' if line['role']=='diluted_weighted_average_shares' else None
        fixed=copy.deepcopy(body);fixed['expense_lines'][0]['cost_driver_slot']='fixed_cost'
        bad=copy.deepcopy(fixed);bad['expense_lines'][0]['cost_driver_unbound_reason']=''
        for line in bad['financial_statement_structure']['lines']:
            if line['role'] in ('diluted_weighted_average_shares','diluted_eps'):line['annual_semantics']='not_applicable'
        h=JointAdmissionTests('runTest');h.addCleanup=self.addCleanup
        f,_,_,_,model,adapter,root=h.fixture(state_body=(state,bad),outputs=[bad,fixed],mission=mission)
        owner=Scheduler(model.scheduler_db);self.addCleanup(owner.close)
        checked=[]
        def full(authority,candidate,**kwargs):
            structure,replay=financial.callback(inputs,checked)(candidate)
            return {'spec_id':company_model_spec_id(candidate['company_ref'],candidate['state_hash'],candidate['task_hash']),
                'financial_input_hash':content_hash(inputs),'financial_structure':structure,'financial_replay':replay}
        config=g.state_dir/'joint-model.json';config.write_text(json.dumps(model.config))
        with patch.object(cli,'CockpitModel',return_value=model),patch.object(cli,'choose_company',return_value=(ACN,state)),patch('dalton_core.statement_original_revenue_context.prepare_model_revenue_inputs',return_value={}),patch('dalton_core.sec_filing_units.prepare_model_unit_inputs',return_value={'unresolved':[],'refreshed':False}),patch.object(CoverageMissionAuthority,'validate_company_model_spec_financials',full):
            out=cli.run_model_spec(state_dir=g.state_dir,model_config_path=config,scheduler_db=model.scheduler_db,
                summary_dir=g.state_dir/'joint-run',company_ref=ACN)
            self.assertEqual(out['spec_status'],'fresh');self.assertEqual(len(out['repair_attempts']),1)
            self.assertEqual(len(adapter.served),2);self.assertEqual(len(checked),2)
            stored=g.missions.latest_company_model_spec(ACN)
            self.assertEqual(stored['work_order_ref'],out['repair_attempts'][0]['work_order_ref'])
            self.assertNotEqual(stored['work_order_ref'],root['work_order_ref'])
            before=[tuple(row) for row in g.missions.connection.execute('SELECT * FROM coverage_mission_company_model_specs')]
            again=cli.run_model_spec(state_dir=g.state_dir,model_config_path=config,scheduler_db=model.scheduler_db,
                summary_dir=g.state_dir/'joint-restart',company_ref=ACN)
            self.assertEqual(again['spec_status'],'duplicate');self.assertEqual(len(adapter.served),2)
            self.assertEqual(before,[tuple(row) for row in g.missions.connection.execute('SELECT * FROM coverage_mission_company_model_specs')])


class JointWakeAndBoundsTests(unittest.TestCase):
    def test_existing_normal_lane_wakes_once_on_new_validation_and_then_stably_holds(self):
        from tests.test_mission_model_spec_lane import ModelSpecLaneTests,FakeMissions,ACN
        from dalton_core.mission_model_spec_lane import MissionModelSpecLaneCoordinator
        h=ModelSpecLaneTests('runTest');h.setUp();self.addCleanup(h.doCleanups)
        lane=MissionModelSpecLaneCoordinator(missions=FakeMissions([ACN]),launcher=h.launcher,mission=lambda:h.mission)
        old={k:v for k,v in cli.model_spec_validation_contract().items() if k!='joint_required_fields'}
        h.launcher.financial_validation_version=old
        first=lane.dispatch_once();h.launcher.finish(first['ticket_ref'],summary={'spec_status':'refused','failure_reason':'expense because missing'})
        self.assertEqual(lane.dispatch_once()['status'],'held')
        h.launcher.financial_validation_version=cli.model_spec_validation_contract()
        again=lane.dispatch_once();self.assertEqual(again['status'],'launched')
        h.launcher.finish(again['ticket_ref'],summary={'spec_status':'refused','failure_reason':'exact remaining source gap'})
        self.assertEqual(lane.dispatch_once()['status'],'held');self.assertEqual(lane.dispatch_once()['status'],'held')
        self.assertEqual(len(h.launcher.started),2)
        self.assertEqual(cli.DEFAULT_REPAIR_ATTEMPTS,1)

    def test_overflow_declines_partial_repair_and_source_tasks_use_each_exact_metric_quarter(self):
        state,bad,_=case()
        template=copy.deepcopy(bad['expense_lines'][0]);bad['expense_lines']=[]
        for i in range(14):
            row=copy.deepcopy(template);row['ref']='expense-'+str(i);bad['expense_lines'].append(row)
        report=joint.from_state(bad,state)
        self.assertEqual(report['status'],'overflow');self.assertEqual(report['findings'],[])
        self.assertIn('fixed finding limit',str(joint.error_for(bad,state)))
        state,body=source_case();state['numeric_context']['cells'].append({'status':'filed','period_shape':'quarter',
            'period_start':'2026-04-01','period_end':'2026-06-30'})
        plans=joint.source_request_plans(body,state,{'root_original':{'work_order_ref':'work:original'}})
        self.assertEqual(len(plans),4)
        self.assertEqual({p['period'] for p in plans},{'quarter_ending:2026-03-31','quarter_ending:2026-06-30'})
        self.assertEqual({p['need']['data_item'] for p in plans},{'Units sold','Average selling price'})
        self.assertEqual(len({(p['need']['data_item'],p['period'])for p in plans}),4)
        self.assertTrue(all(p['acceptance']['model_driver_source_request']['period_start'] in p['need']['question'] for p in plans))
