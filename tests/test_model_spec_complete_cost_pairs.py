"""Complete same-answer pair diagnosis shares the original fixed repair ceiling."""
import copy,json,sqlite3,unittest
from pathlib import Path
from dataclasses import replace
from unittest.mock import patch
from dalton_core import company_model_cli as cli,company_model_spec as specs
from dalton_core import model_spec_typed_field_repair as pairs
from dalton_core import model_spec_maintenance_repair as maintenance
from dalton_core.planner_paid_output_recovery import _authority_connection
from dalton_core.scheduler import Scheduler
from dalton_core.store import content_hash
from tests import test_model_spec_cost_slot_pair_repair as pair_cases
from tests import test_model_spec_maintenance_repair as old_maintenance
from tests import test_model_spec_typed_repair_authority as authority_cases
from tests import test_model_spec_cash_flow_base_repair as calls
from tests import test_cockpit_model_fallback as runtime
from tests import test_model_spec_signed_expense_repair as financial

HISTORICAL=json.loads((Path(__file__).parent/'fixtures/model_spec_repair_contract_v014.json').read_text())


def case(company='company:ticker:cedar'):
    state,bad,_=pair_cases.case('generic');state['company_ref']=company
    bad['expense_lines'][1].update(cost_driver_slot='variable_cost',cost_driver_unbound_reason='No disclosed unit cost for this distinct line.')
    fixed=copy.deepcopy(bad);fixed['expense_lines'][0]['cost_driver_slot']=None
    fixed['expense_lines'][1].pop('cost_driver_unbound_reason')
    return state,bad,fixed


class CompleteCostPairTests(unittest.TestCase):
    def fixture(self,company='company:ticker:cedar',output=None,governed=None):
        f=runtime.CockpitChainTests('test_model_spec_repair_is_a_second_budgeted_scheduler_work');f.setUp();self.addCleanup(f.doCleanups)
        state,bad,fixed=case(company)
        state['numeric_context']={'content_hash':'1'*64,'policy':{},'included_cells':0,
            'omitted_by_series_limit':0,'omitted_by_total_limit':0,'omitted_by_prompt_limit':0,
            'prompt_byte_limit':128000,'prompt_bytes':12000}
        state['state_hash']=content_hash({k:v for k,v in state.items()if k!='state_hash'})
        partial=copy.deepcopy(bad);partial['expense_lines'][0]['cost_driver_slot']=None
        if governed is not None:f.mission=governed.missions._mission_record(governed.mission['id'])
        else:
            f.mission.update(universe=[{'company_ref':company}],deliverables=['company_model'],autonomy={'may_write':['model_run'],'automation_principal':'automation:test'},bindings={'playbook_version':{},'constitution_version':{},'mandate_version':{}},industry_ref='industry:test')
            f.mission['content_hash']=content_hash({k:v for k,v in f.mission.items()if k!='content_hash'})
        class Adapter(runtime.ChainAdapter):
            def __init__(self):super().__init__({});self.outputs=[bad,partial,fixed if output is None else output]
            def execute(self,work,route,profile):
                inv,result=super().execute(work,route,profile);value=self.outputs.pop(0)
                if isinstance(value,BaseException):raise value
                if value=='unknown':return inv,replace(result,status='failed',outputs={},error={'code':'TIMEOUT','message':'sent state unknown'},metadata={**result.metadata,'dispatch_state':'unknown'})
                return inv,replace(result,outputs={'text':json.dumps(value)})
        adapter=Adapter();model=f._model(adapter,policy_version_ref=f.pinned_policy,model_spec_output_tokens=6000)
        root=model.call(purpose='model_spec',request_id=cli.model_spec_request_id(state['state_hash'],repair_config={'max_attempts':1}),prompt=specs.build_prompt(state),mission=f.mission,_model_spec_request_identity=cli.model_spec_request_identity(state['state_hash'],repair_config={'max_attempts':1}))
        self.assertEqual(content_hash(HISTORICAL),maintenance.KNOWN_INCOMPLETE_CONTRACTS[HISTORICAL['ref']])
        with patch.multiple(cli,REPAIR_CONTRACT=HISTORICAL,REPAIR_CONTRACT_REF=HISTORICAL['ref'],REPAIR_CONTRACT_HASH=content_hash(HISTORICAL)),patch.object(pairs,'cost_slot_pair_scope',pairs.legacy_cost_slot_pair_scope):
            attempts=[]
            with self.assertRaises(specs.CompanyModelSpecError):cli._validated_spec_with_repair(model=model,state=state,mission=f.mission,original_call=root,repair_config={'max_attempts':1},decided_by='automation:test',repair_attempts=attempts)
        if governed is None:
            missions=old_maintenance.Missions(f.root/'core.sqlite',f.mission);missions.old_mission=copy.deepcopy(f.mission);self.addCleanup(missions.connection.close)
        else:missions=governed.missions
        authority=maintenance.MaintenanceAuthority(missions,model,state,f.mission)
        with _authority_connection(model.scheduler_db)as c:candidate=authority.find(c)
        self.assertIsNotNone(candidate);self.assertEqual(candidate[0]['contract_ref'],maintenance.PAIR_CONTRACT_REF)
        self.assertEqual(len(adapter.served),2)
        return f,model,adapter,authority,candidate,fixed

    def test_two_company_complete_pair_scope_and_actual_scheduler_single_repair(self):
        for company in ('company:ticker:cedar','company:ticker:maple'):
            state,bad,fixed=case(company);error=pair_cases.error_for(state,bad)
            self.assertEqual([p[1]for p in error.proof['paths'][::2]],[0,1])
            pairs.assert_only_typed_fields_changed(bad,fixed,error.proof,state=state)
            h=authority_cases.TypedRepairAuthorityTests('runTest');h.addCleanup=self.addCleanup
            h._run(lambda:case(company))

    def test_partial_overflow_unknown_or_foreign_pair_and_numeric_drift_rejected(self):
        state,bad,fixed=case();scope=pair_cases.error_for(state,bad).proof
        for kind in ('partial','slot','value','reorder','foreignpath','forgotpath'):
            hostile=copy.deepcopy(fixed);proof=copy.deepcopy(scope)
            if kind=='partial':hostile['expense_lines'][1]=copy.deepcopy(bad['expense_lines'][1])
            if kind=='slot':hostile['expense_lines'][0].update(cost_driver_slot='variable_cost');hostile['expense_lines'][0].pop('cost_driver_unbound_reason')
            if kind=='value':hostile['assessment']='Changed economics'
            if kind=='reorder':hostile['expense_lines'].reverse()
            if kind=='foreignpath':proof['paths'][0][-1]='basis_concept'
            if kind=='forgotpath':proof['paths']=proof['paths'][:2]
            with self.subTest(kind=kind),self.assertRaises(specs.CompanyModelSpecError):pairs.assert_only_typed_fields_changed(bad,hostile,proof,state=state)
        huge=copy.deepcopy(bad);huge['expense_lines']*=8
        self.assertIsNone(pairs.cost_slot_pair_scope(state,huge,0))
        bad['expense_lines'][1]['cost_driver_slot']='unknown'
        self.assertIsNone(pairs.cost_slot_pair_scope(state,bad,0))

    def test_old_one_pair_is_free_replayed_but_cannot_buy_again_without_fixed_owner_grant(self):
        _,model,adapter,authority,candidate,_=self.fixture()
        with self.assertRaises(specs.CompanyModelSpecError):cli._validated_spec_with_repair(model=model,state=authority.state,mission=authority.mission,original_call=candidate[1],repair_config={'max_attempts':5},decided_by='automation:test')
        self.assertEqual(len(adapter.served),2)

    def test_current_fixed_owner_entry_two_companies_restarts_and_cross_day_once(self):
        from datetime import timedelta
        for company in ('company:ticker:cedar','company:ticker:maple'):
            _,model,adapter,authority,candidate,_=self.fixture(company)
            old_prefix=copy.deepcopy(candidate[0]['paid_prefix']);checked=[];attempts=[]
            spec,call=maintenance.execute(authority,candidate,decided_by='automation:test',repairs=attempts,financial_validator=lambda s:checked.append(s))
            self.assertEqual(spec['company_ref'],company);self.assertEqual(len(checked),1);self.assertEqual(len(adapter.served),3)
            model.clock=lambda:runtime.NOW+timedelta(days=1)
            model.config['purpose_call_budgets']['model_spec']['max_output_tokens']=7000
            fresh=maintenance.MaintenanceAuthority(authority.missions,model,authority.state,authority.mission)
            with _authority_connection(model.scheduler_db)as c:again=fresh.find(c)
            self.assertEqual(again[0],candidate[0]);self.assertEqual(again[0]['paid_prefix'],old_prefix)
            replay=maintenance.execute(fresh,again,decided_by='automation:test',repairs=[],financial_validator=lambda s:checked.append(s))
            self.assertEqual(replay[1]['work_order_ref'],call['work_order_ref']);self.assertEqual(len(adapter.served),3)
            self.assertEqual(authority.missions.connection.execute(f'SELECT COUNT(*) FROM {maintenance.TABLE}').fetchone()[0],1)
            self.assertEqual(authority.missions.connection.execute(f'SELECT COUNT(*) FROM {maintenance.CLAIMS}').fetchone()[0],1)

    def test_paid_drift_unknown_or_inflight_never_reopens_fixed_permission(self):
        from datetime import timedelta
        for mode in ('drift','unknown',KeyboardInterrupt('isolated pending attempt')):
            _,model,adapter,authority,candidate,fixed=self.fixture(output=mode)
            if mode=='drift':adapter.outputs[0]=copy.deepcopy(fixed);adapter.outputs[0]['assessment']='Changed'
            with self.assertRaises((specs.CompanyModelSpecError,cli.CockpitModelError,KeyboardInterrupt)):maintenance.execute(authority,candidate,decided_by='automation:test',repairs=[])
            model.config['purpose_call_budgets']['model_spec']['max_output_tokens']=7000
            model.clock=lambda:runtime.NOW+timedelta(days=1)
            with self.assertRaises((specs.CompanyModelSpecError,cli.CockpitModelError)):maintenance.execute(authority,candidate,decided_by='automation:test',repairs=[])
            self.assertEqual(len(adapter.served),3)

    def test_old_annual_grant_and_paid_child_under_014_still_replay_free(self):
        h=old_maintenance.MaintenanceRepairTests('runTest');h.addCleanup=self.addCleanup
        _,model,adapter,authority,candidate,_=h.fixture()
        self.assertEqual(candidate[0]['contract_ref'],maintenance.CONTRACT_REF)
        with patch.multiple(cli,REPAIR_CONTRACT=HISTORICAL,REPAIR_CONTRACT_REF=HISTORICAL['ref'],REPAIR_CONTRACT_HASH=content_hash(HISTORICAL)):
            _,paid=maintenance.execute(authority,candidate,decided_by='automation:test',repairs=[])
        before=[tuple(r)for r in authority.missions.connection.execute(f'SELECT * FROM {maintenance.TABLE}')]
        _,replay=maintenance.execute(authority,candidate,decided_by='automation:test',repairs=[])
        self.assertEqual(replay['work_order_ref'],paid['work_order_ref']);self.assertEqual(len(adapter.served),3)
        self.assertEqual(before,[tuple(r)for r in authority.missions.connection.execute(f'SELECT * FROM {maintenance.TABLE}')])

    def test_rehashed_scope_or_foreign_grant_cannot_admit(self):
        _,model,adapter,authority,candidate,_=self.fixture()
        good,prompt=maintenance.binding_for(candidate[0],candidate[2]['text'])
        for kind in ('scope','contract','root','proof'):
            hostile=copy.deepcopy(good)
            if kind=='scope':hostile['typed_field_proof']['paths'][0][-1]='basis_concept'
            if kind=='contract':hostile['maintenance_grant']['contract_ref']=maintenance.CONTRACT_REF
            if kind=='root':hostile['maintenance_grant']['root_ref']='work:foreign'
            if kind=='proof':hostile['repair_parent']['work_order_hash']='0'*64
            model._model_spec_maintenance_authority=authority
            try:
                with self.subTest(kind=kind),self.assertRaises(cli.CockpitModelError):
                    model.call(purpose='model_spec',mission=authority.mission,request_id='model-spec-repair:'+content_hash(hostile)[:32],prompt=prompt,_structured_output_repair=hostile)
            finally:del model._model_spec_maintenance_authority
            self.assertEqual(len(adapter.served),2)
        self.assertEqual(authority.missions.connection.execute(f'SELECT COUNT(*) FROM {maintenance.CLAIMS}').fetchone()[0],0)

    def test_unsettled_prefix_or_changed_source_permission_cannot_issue_or_send(self):
        for mode in ('state','permission','unsettled'):
            _,model,adapter,authority,candidate,_=self.fixture()
            if mode=='state':authority.state['concepts'].append('invented')
            if mode=='permission':authority.mission['autonomy']['may_write']=[]
            if mode=='unsettled':
                with sqlite3.connect(model.config['budget_db'])as c:
                    for row in c.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='thesis_impact_day_settlements'"):c.execute('DROP TRIGGER '+row[0])
                    c.execute('DELETE FROM thesis_impact_day_settlements')
            with self.assertRaises(ValueError):maintenance.execute(authority,candidate,decided_by='automation:test',repairs=[])
            self.assertEqual(len(adapter.served),2)

    def test_real_financial_dag_still_rejects_after_complete_pair_repair(self):
        state,bad,inputs=financial.case();state['industry_classification']='generic'
        for i in (0,1):bad['expense_lines'][i].update(cost_driver_slot='fixed_cost',cost_driver_unbound_reason='Missing unit basis.')
        fixed=copy.deepcopy(bad)
        for i in (0,1):fixed['expense_lines'][i]['cost_driver_slot']=None
        seen=[];checked=[]
        class Model:
            scheduler_db=None
            def call(self,**kw):seen.append(kw);return {**calls.root_call(fixed),'cost_micros':31}
        with self.assertRaises(specs.CompanyModelSpecError):cli._validated_spec_with_repair(model=Model(),state=state,mission={},original_call=calls.root_call(bad),repair_config={'max_attempts':1},decided_by='automation:test',financial_validator=financial.callback(inputs,checked))
        self.assertEqual(len(seen),1);self.assertEqual(len(checked),1)

    def test_normal_cli_uses_old_prefix_and_full_financial_guard(self):
        from tests.test_company_model_cli import ChooseCompanyTests,ACN
        from dalton_core.company_model_inputs import ModelInputError
        governed=ChooseCompanyTests('test_a_dry_run_reports_the_same_state_the_lane_would_launch_for');governed.setUp();self.addCleanup(governed.doCleanups)
        _,model,adapter,authority,candidate,_=self.fixture(ACN,governed=governed)
        config=governed.state_dir/'pair-maintenance-model.json';config.write_text(json.dumps(model.config));s=Scheduler(model.scheduler_db);self.addCleanup(s.close)
        with patch.object(cli,'CockpitModel',return_value=model),patch.object(cli,'choose_company',return_value=(ACN,authority.state)),patch('dalton_core.statement_original_revenue_context.prepare_model_revenue_inputs',return_value={}),patch('dalton_core.sec_filing_units.prepare_model_unit_inputs',return_value={'unresolved':[],'refreshed':False}),patch.object(cli,'validate_financials_for_repair',side_effect=ModelInputError('full source/history refusal'))as checked:
            out=cli.run_model_spec(state_dir=governed.state_dir,model_config_path=config,scheduler_db=model.scheduler_db,summary_dir=governed.state_dir/'pair-run',company_ref=ACN)
        self.assertEqual(len(adapter.served),3);checked.assert_called_once();self.assertEqual(out['spec_status'],'refused')
        self.assertIn('full source/history refusal',out['failure_reason'])
        self.assertEqual(len(out['repair_attempts']),1);self.assertEqual(out['repair_attempts'][0]['repair_number'],2)
