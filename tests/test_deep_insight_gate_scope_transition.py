import unittest
from copy import deepcopy
from unittest.mock import patch
from tests import test_deep_insight_gate_budget_grant as fixture
from dalton_core.deep_insight_gate_accounting import DeepGateAccounting,COMPLETIONS,SCOPES,_body
from dalton_core.deep_insight_gate_draft import build_group_prompt
from dalton_core.deep_insight_gate_execution import GateExecution,GateExecutionStopped
from dalton_core.deep_insight_gate import GROUPS
from dalton_core.shared_call_budget_policy import updated_shared_limits
from dalton_core.scheduler import Scheduler
from dalton_core.store import content_hash

class ScopeTransitionTests(unittest.TestCase):
    def setUp(self):
        fixture.GateHumanGrantTests.setUp(self)
        p=patch('dalton_core.deep_insight_gate_budget_grant.policy_history',side_effect=lambda _:self.history)
        p.start();self.addCleanup(p.stop)
    setup_source=fixture.GateHumanGrantTests.setup_source
    old_round=fixture.GateHumanGrantTests.old_round
    increase=fixture.GateHumanGrantTests.increase
    account=fixture.GateHumanGrantTests.account
    kw=fixture.GateHumanGrantTests.kw

    def prepare(self,company='company:alpha'):
        self.setup_source(company);self.old_round(('company','thesis'));self.increase()
        old=self.account()
        with patch('dalton_core.deep_insight_gate_budget_grant.policy_history',side_effect=lambda _:self.history):
            self.adapter.script={}
            GateExecution(self.model,old,run_micros=6000000,verifier_micros=500000).stage('normal','company').call(**self.kw('company'))
        return old

    def change_source(self):
        self.inputs=deepcopy(self.inputs)
        self.inputs['thesis']['notes']=['New exact source evidence changed the thesis input']
        self.prompts={g:build_group_prompt(group=g,**fields)for g,fields in self.inputs.items()}
        return self.account()

    def test_two_companies_source_change_inherits_complete_costs_and_free_groups(self):
        for company in ('company:alpha','company:beta'):
            old=self.prepare(company);prior=old.snapshot()[2];sent=len(self.adapter.served)
            new=self.change_source();proof=new.snapshot()[2]
            self.assertTrue(proof['cost_complete'])
            self.assertEqual(proof['accounted_micros'],prior['accounted_micros'])
            self.assertEqual(proof['unknown_work_refs'],prior['unknown_work_refs'])
            with patch('dalton_core.deep_insight_gate_budget_grant.policy_history',side_effect=lambda _:self.history):
                e=GateExecution(self.model,new,run_micros=6000000,verifier_micros=500000)
                for g in GROUPS:e.stage('normal',g).call(**self.kw(g))
            self.assertEqual(len(self.adapter.served),sent+1)
            self.assertGreaterEqual(new.snapshot()[2]['accounted_micros'],prior['accounted_micros'])
            with self.assertRaisesRegex(ValueError,'inherited.*one grant|no eligible failed old step'):
                new.recovery_grant(self.kw('thesis'),group='thesis')

    def test_unrelated_policy_revision_and_consumer_material_do_not_reissue_grant(self):
        old=self.prepare();grant=deepcopy(old.grants['thesis'])
        self.history.insert(0,updated_shared_limits(self.history[0],purpose='ask',kind='call',budget={'max_cost_usd':2},actor_ref='human:test',updated_at=self.clock().isoformat()))
        # Extra material outside the bounded rendered prompt changes consumer
        # provenance, not the paid question. It must not rewrite its grant.
        original_prompt=self.prompts['thesis'];self.inputs['thesis']['material']=[{'unused':'new provenance'}]
        with patch('dalton_core.deep_insight_gate_draft.build_group_prompt',side_effect=lambda group,**kw:self.prompts[group]):
            a=DeepGateAccounting(self.model,self.prompts,company_ref=self.company);a.bind_consumer(self.inputs,wanted=GROUPS)
            with patch('dalton_core.deep_insight_gate_budget_grant.policy_history',side_effect=lambda _:self.history):
                a.import_legacy();got=a.recovery_grant(self.kw('thesis'),group='thesis')
        self.assertEqual(got,grant)

    def test_used_parent_slot_cannot_be_bought_again_after_source_change(self):
        old=self.prepare()
        with patch('dalton_core.deep_insight_gate_budget_grant.policy_history',side_effect=lambda _:self.history):
            self.adapter.script={'profile:gpt-6-astra':{'code':'TIMEOUT','message':'timeout'}}
            with self.assertRaises(GateExecutionStopped):GateExecution(self.model,old,run_micros=6000000,verifier_micros=0).stage('normal','thesis').call(**self.kw('thesis'))
        new=self.change_source();sent=len(self.adapter.served)
        with patch('dalton_core.deep_insight_gate_budget_grant.policy_history',side_effect=lambda _:self.history):
            with self.assertRaisesRegex(ValueError,'inherited.*one grant|no eligible failed old step'):new.recovery_grant(self.kw('thesis'),group='thesis')
        self.assertEqual(len(self.adapter.served),sent)

    def test_incomplete_or_tampered_parent_does_not_omit_historical_fees(self):
        old=self.prepare()
        with Scheduler(self.model.scheduler_db)as scheduler:
            # A failed hash check is an isolated negative fixture, never a
            # production mutation or a weakened strong-reader path.
            scheduler.connection.execute(f'DROP TRIGGER {COMPLETIONS}_update_immutable')
            scheduler.connection.execute(f'UPDATE {COMPLETIONS} SET content_hash=? WHERE scope_ref_binding=?',('0'*64,old.ref))
        with self.assertRaisesRegex(ValueError,'hash differs'):self.change_source()

    def test_parent_cycle_and_omission_refuse(self):
        old=self.prepare();new=self.change_source()
        from dalton_core.deep_insight_gate_scope_transition import derive,verified_parent
        from dalton_core.planner_paid_output_recovery import _authority_connection,recovery_readers
        with _authority_connection(self.model.scheduler_db)as c,recovery_readers(self.model.config['model_router_db'],self.model.config['budget_db'],now=self.model.clock())as(router,budget):
            with self.assertRaisesRegex(ValueError,'cycle'):verified_parent(c,router,budget,self.model,old.ref,path=(old.ref,))
            with self.assertRaisesRegex(ValueError,'census omits'):derive(c,router,budget,self.model,scope=new.scope,ref=new.ref,consumer=new.consumer,parents=[])

    def test_inherited_correction_slot_is_not_reset_by_new_material(self):
        old=self.prepare()
        kw={**self.kw('market'),'request_id':'numbers-correction-old','prompt':'Frozen market correction'}
        old.register(role='numbers',group='market',kwargs=kw)
        new=self.change_source()
        with self.assertRaisesRegex(ValueError,'generation is already used'):
            new.register(role='numbers',group='market',kwargs={**kw,'request_id':'numbers-correction-new'})

    def test_rehashed_fresh_flag_cannot_hide_old_legacy_fees(self):
        from dalton_core.store import canonical_json
        old=self.prepare()
        with Scheduler(self.model.scheduler_db)as scheduler:
            scheduler.connection.execute(f'DROP TRIGGER {SCOPES}_update_immutable')
            row=scheduler.connection.execute(f'SELECT * FROM {SCOPES} WHERE scope_ref=?',(old.ref,)).fetchone()
            body={**_body(row),'complete':True}
            scheduler.connection.execute(f'UPDATE {SCOPES} SET record_json=?,content_hash=? WHERE scope_ref=?',(canonical_json(body),content_hash(body),old.ref))
        with self.assertRaisesRegex(ValueError,'pre-registration legacy Work'):self.change_source()

    def test_new_unknown_correction_fee_does_not_receive_old_grant_permission(self):
        from dalton_core.cockpit_model import CockpitModelError
        old=self.prepare()
        kw={**self.kw('market'),'request_id':'numbers-correction-unknown','prompt':'Frozen market correction'}
        old.register(role='numbers',group='market',kwargs=kw)
        self.adapter.script={'profile:gpt-6-astra':{'code':'TIMEOUT','message':'timeout'}}
        with self.assertRaises(CockpitModelError):self.model.call(**kw)
        new=self.change_source();sent=len(self.adapter.served)
        with self.assertRaisesRegex(ValueError,'systemic unknown'):
            new.before_paid(run_micros=6000000,next_call_micros=500000,mission=self.mission)
        self.assertEqual(len(self.adapter.served),sent)

    def test_future_scopes_do_not_change_existing_complete_parent_census(self):
        self.prepare();current=self.change_source();prior=current.snapshot()[2]
        for n in range(20):
            later=deepcopy(self.inputs);later['thesis']['notes']=['Later distinct source '+str(n)]
            prompts={g:build_group_prompt(group=g,**fields)for g,fields in later.items()}
            DeepGateAccounting(self.model,prompts,company_ref=self.company)
        self.assertEqual(current.snapshot()[2]['bills'],prior['bills'])
        self.assertTrue(current.snapshot()[2]['cost_complete'])

    def test_current_consumer_cannot_bind_foreign_company_in_one_group(self):
        self.prepare();self.inputs=deepcopy(self.inputs)
        self.inputs['thesis']['company']={'company_ref':'company:foreign'}
        self.prompts={g:build_group_prompt(group=g,**fields)for g,fields in self.inputs.items()}
        with self.assertRaisesRegex(ValueError,'consumer company differs'):self.account()

    def test_lane_validation_marker_does_not_change_paid_group_request(self):
        from dalton_core.deep_insight_gate_cli import GATE_DRAFTING_CONTRACT
        from dalton_core.deep_insight_gate_accounting import normal_request_id
        self.setup_source('company:alpha')
        before=deepcopy(self.prompts)
        ids={g:normal_request_id(g,self.company,p)for g,p in before.items()}
        with patch('dalton_core.deep_insight_gate_cli.GATE_DRAFTING_CONTRACT','old-marker'):
            after={g:build_group_prompt(group=g,**fields)for g,fields in self.inputs.items()}
        self.assertEqual(after,before)
        self.assertEqual({g:normal_request_id(g,self.company,p)for g,p in after.items()},ids)
        self.assertIn('indexed-source-scope-lineage',GATE_DRAFTING_CONTRACT)
