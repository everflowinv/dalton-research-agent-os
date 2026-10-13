"""Fixed explicit grant with real Scheduler, router and budget admissions."""
import copy
import json
import sqlite3
import unittest
from contextlib import contextmanager,closing
from dataclasses import replace
from unittest.mock import patch
from dalton_core import company_model_cli as cli
from dalton_core import model_spec_maintenance_repair as maintenance
from dalton_core.company_model_spec import CompanyModelSpecError,build_prompt
from dalton_core.cockpit_model import CockpitModelError
from dalton_core.scheduler import Scheduler
from dalton_core.store import canonical_json,content_hash
from dalton_core.planner_paid_output_recovery import _authority_connection
from tests.test_model_spec_complete_diagnostics import case
from tests import test_cockpit_model_fallback as runtime


# Original 0.12 contract bytes: never derive historical authority from the
# current repair contract. The unchanged owner-pinned digest below proves it.
_HISTORICAL_REPAIR_CONTRACT_012 = json.loads(r'''
{
  "ref": "contract:company-model-spec-structured-output-repair:0.12",
  "eligible_error_codes": [
    "format",
    "structure",
    "text_length",
    "cash_flow_operating_base_shape",
    "signed_filed_income_sum_coefficient",
    "operating_metric_periodicity",
    "acquisitions_forecast_method",
    "revenue_driver_identity",
    "expense_cost_slot_pair"
  ],
  "eligible_structure_rules_ref": "rule:company-model-spec-repairable-structure:0.4",
  "eligible_structure_rules": [
    "a derived line cannot claim a filed concept",
    "a filed subtotal may only be actual/tie authority or unavailable",
    "diluted weighted-average shares require duration direct_annual shares",
    "only diluted weighted-average shares may carry annual_forecast_method",
    "Only diluted weighted-average shares may select",
    "sum formula roles do not match its company statement output",
    "EPS denominator must be diluted weighted-average shares",
    "EPS numerator must use the company-specific diluted EPS numerator role",
    "company-presented components must be filed and subtotals must be derived",
    "diluted EPS cannot be aggregated from quarterly EPS"
  ],
  "validation": "full_spec_from_response_and_supplied_financial_validator_after_every_attempt",
  "text_length_change": "only_overlong_schema_strings_may_change",
  "structure_change": "only_the_named_rule_may_change",
  "eps_numerator_bridge": {
    "ref": "company-model-spec-accounting-eps-bridge:0.1",
    "guidance": "Keep accounting net_income separate from diluted_eps_numerator. When net income is calculated from pretax income, taxes and filed company components, retain that line and its formula with role net_income; do not retag it diluted_eps_numerator. Add a separate derived numerator bridge using the exact net_income or parent_net_income line and only disclosed dilution adjustments. If the filed diluted EPS proves that net income itself is the numerator, the bridge is sum(+1 * net-income), and the EPS divide refers to this bridge and the filed diluted shares. A numerator sum cannot directly use pretax_income or income_tax_expense. Tie the accounting total and numerator to their exact filed authorities and validate the numerator through filed diluted EPS in every period. Missing dilution evidence remains unavailable, never zero.",
    "change": "one_derived_numerator_bridge_and_its_consuming_eps_formula_only",
    "accounting_line": "retain_or_restore_accounting_net_income_role_and_formula"
  },
  "filed_subtotal_placement": {
    "ref": "model-spec-filed-subtotal-concept-placement:0.1",
    "change": "same_line_concept_to_matching_formula_tie_only",
    "source_identity": "exact_filed_concept_and_evidence_remain_unchanged"
  },
  "cash_flow_operating_base_shape": {
    "ref": "rule:cash-flow-operating-base-unavailable-repair:0.1",
    "allowed_fields": [
      "forecast_method",
      "forecast_base_ref",
      "because"
    ],
    "change": "operating_cash_flow_only_to_unavailable_and_null_base",
    "preserve": "all_other_fields_including_filed_concept_and_fcf_formula",
    "scope_validation": "exact_parent_and_repaired_body_comparison"
  },
  "signed_filed_income_sum": {
    "ref": "model-spec-signed-filed-income-sum-coefficient:0.1",
    "scope": "one_exact_coefficient_only_with_all_held_periods_strictly_proven",
    "validation": "full_financial_validation_after_every_new_or_replayed_answer"
  },
  "joint_signed_filed_income_sum": {
    "ref": "model-spec-signed-filed-income-sum-coefficients:0.2",
    "scope": "two_to_four_exact_coefficients_unique_all_held_periods_full_DAG_proven",
    "attempts": "shares_existing_single_repair_limit"
  },
  "typed_field_change": {
    "ref": "model-spec-typed-field-repair:0.1",
    "scope": "exact_parent_paths_only_with_source_group_reconstruction_for_identity",
    "validation": "full_spec_and_financial_validation_after_every_new_or_replayed_answer"
  },
  "cost_slot_exclusive_pair": {
    "ref": "model-spec-cost-slot-exclusive-pair:0.1",
    "scope": "one_exact_expense_pair_only_original_slot_or_explicit_unbound",
    "preserve": "all_other_parent_fields_and_all_financial_source_authorities"
  },
  "semantic_failure": "whole_refusal",
  "prior_contract_repair": "model-spec-repair-replay-by-parent-and-error:0.1"
}
''')

class Missions:
    """Owner transaction only is stubbed; model authority/fees are all real."""
    def __init__(self,path,mission):
        self.connection=sqlite3.connect(path);self.connection.row_factory=sqlite3.Row
        self.authorized=False;self.mission=mission
        self.connection.create_function('dalton_coverage_mission_authorized',0,lambda:int(self.authorized))
    @contextmanager
    def _transaction(self):
        self.authorized=True
        try:
            with self.connection: yield self.connection.cursor()
        finally:self.authorized=False
    def _resolve_active_mission_for_company(self,company,**kwargs):
        if (kwargs['mission_version_ref'],kwargs['mission_version_hash'])!=(self.mission['id'],self.mission['content_hash']):raise ValueError('inactive mission')
        return self.mission
    def _mission_record(self,ref):
        return self.old_mission if ref==self.old_mission['id'] else self.mission
    def _validate_playbook_binding(self,*args):pass
    def _validate_constitution_binding(self,*args):pass
    def _validate_mandate_binding(self,*args):pass


class MaintenanceRepairTests(unittest.TestCase):
    def test_historical_contract_stays_exact_when_current_contract_evolves(self):
        original=copy.deepcopy(_HISTORICAL_REPAIR_CONTRACT_012)
        expected=maintenance.KNOWN_INCOMPLETE_CONTRACTS[original['ref']]
        self.assertEqual(content_hash(original),expected)
        self.assertNotIn('cash_flow_ratio_applicability',original)
        self.assertNotIn(cli.CASH_RATIO_CODE,original['eligible_error_codes'])
        with patch.object(cli,'REPAIR_CONTRACT',{'ref':'future:999','new_field':'unrelated'}):
            self.assertEqual(content_hash(_HISTORICAL_REPAIR_CONTRACT_012),expected)
        # Retagging a current capability as historical is not valid authority.
        forged=copy.deepcopy(original)
        forged['cash_flow_ratio_applicability']=cli.REPAIR_CONTRACT['cash_flow_ratio_applicability']
        self.assertNotEqual(content_hash(forged),expected)

    def fixture(self,company='company:ticker:cedar',output=None,governed=None):
        f=runtime.CockpitChainTests('test_model_spec_repair_is_a_second_budgeted_scheduler_work');f.setUp();self.addCleanup(f.doCleanups)
        state,bad,fixed=case(company)
        state['numeric_context']={'content_hash':'1'*64,'policy':{},'included_cells':0,
            'omitted_by_series_limit':0,'omitted_by_total_limit':0,'omitted_by_prompt_limit':0,
            'prompt_byte_limit':128000,'prompt_bytes':12000}
        state['state_hash']=content_hash({k:v for k,v in state.items()if k!='state_hash'})
        partial=copy.deepcopy(bad)
        for v in partial['financial_statement_structure']['lines']:
            if v['role']=='diluted_weighted_average_shares':v['annual_semantics']='direct_annual'
        if governed is not None:
            f.mission=governed.missions._mission_record(governed.mission["id"])
        else:
            f.mission.update(universe=[{'company_ref':company}],deliverables=['company_model'],autonomy={'may_write':['model_run'],'automation_principal':'automation:test'},
            bindings={'playbook_version':{},'constitution_version':{},'mandate_version':{}},industry_ref='industry:test')
        if governed is None: f.mission['content_hash']=content_hash({k:v for k,v in f.mission.items()if k!='content_hash'})
        class Adapter(runtime.ChainAdapter):
            def __init__(self):super().__init__({});self.outputs=[bad,partial,fixed if output is None else output]
            def execute(self,work,route,profile):
                inv,result=super().execute(work,route,profile)
                value=self.outputs.pop(0)
                if isinstance(value,BaseException):raise value
                if value=='unknown':return inv,replace(result,status='failed',outputs={},error={'code':'TIMEOUT','message':'unknown sent state'},metadata={**result.metadata,'dispatch_state':'unknown'})
                return inv,replace(result,outputs={'text':json.dumps(value)})
        adapter=Adapter();model=f._model(adapter,policy_version_ref=f.pinned_policy,model_spec_output_tokens=6000)
        root=model.call(purpose='model_spec',request_id=cli.model_spec_request_id(state['state_hash'],repair_config={'max_attempts':1}),
            prompt=build_prompt(state),mission=f.mission,_model_spec_request_identity=cli.model_spec_request_identity(state['state_hash'],repair_config={'max_attempts':1}))
        old_contract=copy.deepcopy(_HISTORICAL_REPAIR_CONTRACT_012)
        self.assertEqual(content_hash(old_contract),maintenance.KNOWN_INCOMPLETE_CONTRACTS[old_contract['ref']])
        with patch.multiple(cli,REPAIR_CONTRACT=old_contract,REPAIR_CONTRACT_REF=old_contract['ref'],REPAIR_CONTRACT_HASH=content_hash(old_contract)),patch.object(cli,'complete_diagnostics_error',return_value=None):
            attempts=[]
            with self.assertRaises(CompanyModelSpecError):
                cli._validated_spec_with_repair(model=model,state=state,mission=f.mission,original_call=root,
                    repair_config={'max_attempts':1},decided_by='automation:test',repair_attempts=attempts)
        self.assertEqual(len(adapter.served),2)
        old=copy.deepcopy(f.mission)
        f.mission=copy.deepcopy(f.mission)
        if governed is None: f.mission['id']='coverage-mission-version:p14m:2';f.mission['content_hash']=content_hash({k:v for k,v in f.mission.items()if k!='content_hash'})
        if governed is None:
            missions=Missions(f.root/'core.sqlite',f.mission);missions.old_mission=old;self.addCleanup(missions.connection.close)
        else:
            missions=governed.missions
        authority=maintenance.MaintenanceAuthority(missions,model,state,f.mission)
        with _authority_connection(model.scheduler_db)as c:candidate=authority.find(c)
        if candidate is None:
            with _authority_connection(model.scheduler_db)as c:
                maintenance.qualify(c,missions=missions,model=model,state=state,mission=f.mission,root_ref=root["work_order_ref"],parent_ref=attempts[0]["work_order_ref"])
        self.assertIsNotNone(candidate)
        return f,model,adapter,authority,candidate,fixed

    def test_two_companies_current_mission_normal_admission_once_then_free_replay(self):
        for company in ('company:ticker:cedar','company:ticker:maple'):
            f,model,adapter,authority,candidate,fixed=self.fixture(company)
            checked=[];attempts=[]
            spec,call=maintenance.execute(authority,candidate,decided_by='automation:test',repairs=attempts,financial_validator=lambda s:checked.append(s['company_ref']))
            self.assertEqual(spec['company_ref'],company);self.assertEqual(checked,[company]);self.assertEqual(len(adapter.served),3)
            before=authority.missions.connection.execute(f'SELECT * FROM {maintenance.TABLE}').fetchall()
            maintenance.execute(authority,candidate,decided_by='automation:test',repairs=[],financial_validator=lambda s:checked.append(s['company_ref']))
            self.assertEqual(len(adapter.served),3);self.assertEqual(len(checked),2)
            self.assertEqual(before,authority.missions.connection.execute(f'SELECT * FROM {maintenance.TABLE}').fetchall())
            self.assertGreater(attempts[0]['cost_micros'],0)
            with Scheduler(model.scheduler_db)as s:
                child=s.work_order_authority(call['work_order_ref'])['work_order']
                self.assertEqual(child['metadata']['mission_version_ref'],f.mission['id'])
            self.assertTrue(all(v['accounted_micros']>0 for v in candidate[0]['paid_prefix']))

    def test_paid_semantic_drift_cannot_buy_again_under_new_budget(self):
        _,model,adapter,authority,candidate,fixed=self.fixture()
        adapter.outputs[0]=copy.deepcopy(fixed);adapter.outputs[0]['assessment']='Different economic judgement'
        with self.assertRaisesRegex(CompanyModelSpecError,'outside its exact'):
            maintenance.execute(authority,candidate,decided_by='automation:test',repairs=[])
        f=authority.missions
        model.config['purpose_call_budgets']['model_spec']['max_output_tokens']=7000
        with self.assertRaises(CompanyModelSpecError):maintenance.execute(authority,candidate,decided_by='automation:test',repairs=[])
        self.assertEqual(len(adapter.served),3)

    def test_unknown_or_inflight_claim_does_not_reset_with_budget_or_day(self):
        for status in ('unknown',KeyboardInterrupt('isolated in-flight crash')):
            _,model,adapter,authority,candidate,_=self.fixture(output=status)
            with self.assertRaises((CockpitModelError,KeyboardInterrupt)):
                maintenance.execute(authority,candidate,decided_by='automation:test',repairs=[])
            model.config['purpose_call_budgets']['model_spec']['max_output_tokens']=7000
            with self.assertRaises((CompanyModelSpecError,CockpitModelError)):maintenance.execute(authority,candidate,decided_by='automation:test',repairs=[])
            self.assertEqual(len(adapter.served),3)

    def test_grant_metadata_without_live_owner_cannot_admit(self):
        _,model,adapter,authority,candidate,_=self.fixture();binding,prompt=maintenance.binding_for(candidate[0],candidate[2]['text'])
        with self.assertRaisesRegex(CockpitModelError,'live owner'):
            model.call(purpose='model_spec',mission=authority.mission,request_id='model-spec-repair:'+content_hash(binding)[:32],prompt=prompt,_structured_output_repair=binding)
        self.assertEqual(len(adapter.served),2)

    def test_current_permission_revoked_or_full_state_changed_refuses(self):
        for change in ('scope','universe','state','mission'):
            _,model,adapter,authority,candidate,_=self.fixture()
            if change=='scope':authority.mission['autonomy']['may_write']=[]
            if change=='universe':authority.mission['universe']=[]
            if change=='state':authority.state['company_ref']='company:ticker:foreign'
            if change=='mission':authority.mission['id']='coverage-mission-version:p14m:missing'
            with self.assertRaises((ValueError,CockpitModelError)):
                maintenance.execute(authority,candidate,decided_by='automation:test',repairs=[])
            self.assertEqual(len(adapter.served),2)

    def test_owner_grant_immutable_and_capacity_prevents_extra_child(self):
        _,model,adapter,authority,candidate,_=self.fixture()
        with self.assertRaises(sqlite3.IntegrityError):authority.missions.connection.execute(f'UPDATE {maintenance.TABLE} SET content_hash=?',('0'*64,))
        binding,prompt=maintenance.binding_for(candidate[0],candidate[2]['text']);binding['diagnostic_bundle']['findings'][0]['choices']=[['invented']]
        with self.assertRaises(CockpitModelError):
            model._model_spec_maintenance_authority=authority
            try:model.call(purpose='model_spec',mission=authority.mission,request_id='model-spec-repair:'+content_hash(binding)[:32],prompt=prompt,_structured_output_repair=binding)
            finally:del model._model_spec_maintenance_authority
        self.assertEqual(len(adapter.served),2)

    def test_no_send_budget_refusal_defers_unused_grant_under_normal_new_policy(self):
        from dalton_core.thesis_impact_budget import ThesisImpactBudgetStore
        f,model,adapter,authority,candidate,_=self.fixture()
        with ThesisImpactBudgetStore(f.root/'budget.sqlite')as budget:
            budget.register_policy(policy_version_id='thesis-impact-day-budget-policy:maintenance-low:1',day_cap_micros=1)
        model.config['budget_policy_ref']='thesis-impact-day-budget-policy:maintenance-low:1'
        with self.assertRaises(CockpitModelError):
            maintenance.execute(authority,candidate,decided_by='automation:test',repairs=[])
        self.assertEqual(len(adapter.served),2)
        claims=authority.missions.connection.execute(f'SELECT * FROM {maintenance.CLAIMS}').fetchall()
        self.assertEqual(len(claims),1)
        with _authority_connection(model.scheduler_db)as connection:
            self.assertTrue(maintenance._no_send(connection,model,claims[0]))
        model.config['budget_policy_ref']=runtime.BUDGET_POLICY
        # A later ordinary budget day may defer only this proved unused grant;
        # the old no-send formal is immutable and no paid/day entitlement resets.
        from datetime import timedelta
        model.clock=lambda:runtime.NOW+timedelta(days=1)
        maintenance.execute(authority,candidate,decided_by='automation:test',repairs=[])
        self.assertEqual(len(adapter.served),3)
        self.assertEqual(authority.missions.connection.execute(f'SELECT COUNT(*) FROM {maintenance.CLAIMS}').fetchone()[0],2)

    def test_current_repair_disabled_retains_original_paid_prefix(self):
        _,model,adapter,authority,candidate,_=self.fixture()
        model.config['structured_output_repair']={'max_attempts':0}
        with self.assertRaisesRegex(ValueError,'disabled'):
            maintenance.execute(authority,candidate,decided_by='automation:test',repairs=[])
        self.assertEqual(len(adapter.served),2)

    def test_original_formal_or_receipt_tamper_cannot_authorize_maintenance(self):
        f,model,adapter,authority,candidate,_=self.fixture()
        # Tamper in an isolated fixture after disabling its immutability
        # trigger: a self-consistent Work alone is insufficient.
        with closing(sqlite3.connect(model.scheduler_db))as c:
            for row in c.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='scheduler_result_envelopes'").fetchall():
                c.execute('DROP TRIGGER '+row[0])
            c.execute('UPDATE scheduler_result_envelopes SET content_hash=? WHERE work_order_id=?',('0'*64,candidate[1]['work_order_ref']))
            c.commit()
        with self.assertRaises(ValueError):
            maintenance.execute(authority,candidate,decided_by='automation:test',repairs=[])
        self.assertEqual(len(adapter.served),2)

    def test_normal_cli_owner_entry_uses_paid_prefix_and_keeps_full_financial_refusal(self):
        from tests.test_company_model_cli import ChooseCompanyTests,ACN
        from dalton_core.company_model_inputs import ModelInputError
        governed=ChooseCompanyTests('test_a_dry_run_reports_the_same_state_the_lane_would_launch_for')
        governed.setUp();self.addCleanup(governed.doCleanups)
        f,model,adapter,authority,candidate,_=self.fixture(company=ACN,governed=governed)
        config=governed.state_dir/'maintenance-model.json';config.write_text(json.dumps(model.config))
        live_scheduler=Scheduler(model.scheduler_db);self.addCleanup(live_scheduler.close)
        # Only the synthetic material projection is stubbed. Current mission,
        # governor transaction, model Work/formal/router/day budget are real.
        # The final financial check deliberately refuses, proving that the
        # maintenance setting correction cannot certify financial history.
        with patch.object(cli,'CockpitModel',return_value=model),patch.object(cli,'choose_company',return_value=(ACN,authority.state)), \
             patch('dalton_core.statement_original_revenue_context.prepare_model_revenue_inputs',return_value={}), \
             patch('dalton_core.sec_filing_units.prepare_model_unit_inputs',return_value={'unresolved':[],'refreshed':False}), \
             patch.object(cli,'validate_financials_for_repair',side_effect=ModelInputError('isolated full financial refusal'))as checked:
            result=cli.run_model_spec(state_dir=governed.state_dir,model_config_path=config,
                scheduler_db=model.scheduler_db,summary_dir=governed.state_dir/'maintenance-run',company_ref=ACN)
        self.assertEqual(len(adapter.served),3);checked.assert_called_once()
        self.assertEqual(result['spec_status'],'refused');self.assertIn('isolated full financial refusal',result['failure_reason'])
        self.assertEqual(len(result['repair_attempts']),1)
        self.assertEqual(result['repair_attempts'][0]['repair_number'],2)
        self.assertGreater(result['repair_attempts'][0]['cost_micros'],0)
        self.assertEqual(governed.missions.company_model_specs(ACN),[])

    def test_newer_exact_root_unknown_prevents_borrowing_older_successful_repair(self):
        from datetime import timedelta
        _,model,adapter,authority,candidate,_=self.fixture()
        model.clock=lambda:runtime.NOW+timedelta(seconds=1)
        model.config['purpose_call_budgets']['model_spec']['max_output_tokens']=7000
        adapter.outputs=['unknown']
        with self.assertRaises(CockpitModelError):
            model.call(purpose='model_spec',mission=authority.mission,
                request_id=cli.model_spec_request_id(authority.state['state_hash'],repair_config={'max_attempts':1}),
                prompt=build_prompt(authority.state),
                _model_spec_request_identity=cli.model_spec_request_identity(authority.state['state_hash'],repair_config={'max_attempts':1}))
        with _authority_connection(model.scheduler_db)as connection:
            self.assertIsNone(authority.find(connection))
        self.assertEqual(len(adapter.served),3)
