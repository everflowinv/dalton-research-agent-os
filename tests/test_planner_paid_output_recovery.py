"""Settled synthetic provider calls exercise the normal scheduler and budget."""
import json
import copy
import sqlite3
import unittest
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from tests import test_cockpit_model_fallback as chain
from tests import test_model_output_contract_hold as overrun
from dalton_core.cockpit_model import CockpitModelError, CockpitModel
from dalton_core.contracts import InvocationGranularity
from dalton_core.model_router import ModelRouter
from dalton_core.planner_paid_output_recovery import recovery_for_work, planner_candidate, recovery_readers, MARKER
from dalton_core.store import content_hash, canonical_json, DaltonStore
from dalton_core.thesis_impact_budget import ThesisImpactBudgetStore


class BothOverrun(overrun.PaidOverrun):
    def execute(self, work, route, profile):
        inv, result = super().execute(work, route, profile)
        if result.status == 'failed':
            return inv, result
        digest = content_hash({'second_synthetic_response':work.id})
        usage = {'thinking_level':route['thinking_level'], 'output_tokens':work.budget['max_output_tokens']+1,
            'metering_source':'provider_reported', 'raw_provider_telemetry':{'cost':{'available':True,'usd':.001},'response_content_hash':digest}}
        inv = replace(inv, usage=usage, runtime_ref=profile['adapter_ref'], actor_ref='runtime:openclaw-model-broker',
            granularity=InvocationGranularity.WORK_ORDER, created_at=route['created_at'], completed_at=route['created_at'])
        result = replace(result, status='failed', created_at=route['created_at'], outputs={},
            error={'code':'PROVIDER_BUDGET_EXCEEDED','source':'openclaw-model-adapter',
                'message':'provider max_output_tokens telemetry exceeds WorkOrder budget'},
            metadata={'route_decision_ref':route['id'],'profile_version_ref':profile['profile_version_ref'],
                'thinking_level':route['thinking_level'],'broker_response_hash':digest,
                'runtime_version':'synthetic-host:1','broker_version':'synthetic-broker:1','dispatch_proof':None})
        return inv, result


class PlanAdapter(overrun.PaidOverrun):
    def execute(self, work, route, profile):
        invocation, result = super().execute(work, route, profile)
        if result.status == 'succeeded':
            result = replace(result, outputs={'text':json.dumps({'schema_version':'0.1',
                'assessment':'Synthetic fixture has no additional directed question.',
                'directives':[], 'inquiries':[], 'sufficiency':[]})})
        return invocation, result


class PlannerRecoveryTests(unittest.TestCase):
    def setUp(self):
        chain.CockpitChainTests.setUp(self)
    _model = chain.CockpitChainTests._model

    def model(self, adapter):
        return self._model(adapter, policy_version_ref=self.chain_policy,
                           clock=lambda:datetime.now(timezone.utc))

    def first(self, adapter=None, key='same-state'):
        adapter = adapter or overrun.PaidOverrun()
        model = self.model(adapter)
        with self.assertRaises(CockpitModelError):
            model.call(purpose='plan',request_id=key,prompt='same synthetic state',mission=self.mission)
        return model, adapter

    def test_exact_same_state_recovers_once_and_old_failure_fee_stays_unchanged(self):
        model, adapter = self.first()
        with sqlite3.connect(self.root/'scheduler.sqlite') as c:
            old = c.execute('SELECT * FROM scheduler_formal_results').fetchall()
        answer = model.call(purpose='plan', request_id='same-state',prompt='same synthetic state',mission=self.mission)
        self.assertIn('claude', answer['text'])
        for _ in range(3):
            self.assertTrue(model.call(purpose='plan',request_id='same-state',prompt='same synthetic state',mission=self.mission)['replayed'])
        self.assertEqual(adapter.served,['profile:gpt-6-astra','profile:claude-fable-5-1'])
        with sqlite3.connect(self.root/'scheduler.sqlite') as c:
            self.assertEqual(c.execute('SELECT * FROM scheduler_formal_results WHERE work_order_id=?',(old[0][1],)).fetchall(),old)
            rows = [json.loads(r[0]) for r in c.execute('SELECT work_order_json FROM scheduler_work_orders')]
        self.assertEqual(len(rows),2)
        self.assertEqual(rows[0]['budget'],rows[1]['budget'])
        self.assertIn(MARKER,rows[1]['metadata']['request_id'])
        with recovery_readers(self.router_db,self.root/'budget.sqlite',now=datetime.now(timezone.utc)) as (_,budget):
            self.assertEqual([r[0] for r in budget.connection.execute('SELECT actual_micros FROM thesis_impact_day_settlements ORDER BY created_at')],[600000,1000])

    def test_expired_hold_cannot_create_new_paid_work(self):
        model, adapter = self.first()
        with ModelRouter(self.router_db,read_only=True) as router:
            hold=json.loads(router.connection.execute('SELECT record_json FROM model_output_contract_holds').fetchone()[0])
        model.clock=lambda:datetime.fromisoformat(hold['until'])+timedelta(seconds=1)
        with self.assertRaises(CockpitModelError):
            model.call(purpose='plan',request_id='same-state',prompt='same synthetic state',mission=self.mission)
        self.assertEqual(len(adapter.served),1)

    def test_successful_successor_still_replays_after_hold_expiry(self):
        model, adapter = self.first()
        model.call(purpose='plan',request_id='same-state',prompt='same synthetic state',mission=self.mission)
        with ModelRouter(self.router_db,read_only=True) as router:
            hold=json.loads(router.connection.execute('SELECT record_json FROM model_output_contract_holds').fetchone()[0])
        model.clock=lambda:datetime.fromisoformat(hold['until'])+timedelta(seconds=1)
        self.assertTrue(model.call(purpose='plan',request_id='same-state',prompt='same synthetic state',mission=self.mission)['replayed'])
        self.assertEqual(len(adapter.served),2)

    def test_second_model_failure_is_terminal_not_an_unbounded_third_call(self):
        model, adapter = self.first(BothOverrun())
        for _ in range(3):
            with self.assertRaises(CockpitModelError):
                model.call(purpose='plan',request_id='same-state',prompt='same synthetic state',mission=self.mission)
        self.assertEqual(adapter.served,['profile:gpt-6-astra','profile:claude-fable-5-1'])

    def test_missing_settlement_never_reenters_or_treats_reserved_fee_as_actual(self):
        model, adapter = self.first()
        with sqlite3.connect(self.root/'budget.sqlite') as c:
            for name, in c.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='thesis_impact_day_settlements'").fetchall():
                c.execute('DROP TRIGGER '+name)
            c.execute('DELETE FROM thesis_impact_day_settlements')
        with self.assertRaises(CockpitModelError):
            model.call(purpose='plan',request_id='same-state',prompt='same synthetic state',mission=self.mission)
        self.assertEqual(len(adapter.served),1)

    def test_unknown_dispatch_and_wrong_purpose_do_not_recover(self):
        model, adapter = self.first(overrun.PaidOverrun(variant='unknown_dispatch'))
        with self.assertRaises(CockpitModelError):
            model.call(purpose='plan',request_id='same-state',prompt='same synthetic state',mission=self.mission)
        self.assertEqual(len(adapter.served),1)

    def test_foreign_mission_or_changed_cap_cannot_borrow_the_hold(self):
        model, adapter = self.first()
        work=adapter.proofs[0][0]
        with sqlite3.connect(self.root/'scheduler.sqlite') as c, recovery_readers(self.router_db,self.root/'budget.sqlite',now=datetime.now(timezone.utc)) as (router,budget):
            c.row_factory=sqlite3.Row
            args=dict(work_ref=work.id,mission_version_ref=self.mission['id'],policy_ref=self.chain_policy,
                      expected_budget=work.budget,now=datetime.now(timezone.utc))
            self.assertIsNotNone(recovery_for_work(c,router,budget,**args))
            self.assertIsNone(recovery_for_work(c,router,budget,**{**args,'mission_version_ref':'mission:foreign'}))
            self.assertIsNone(recovery_for_work(c,router,budget,**{**args,'expected_budget':{**work.budget,'max_output_tokens':9999}}))

    def test_formal_hash_drift_is_strongly_refused_without_another_call(self):
        model, adapter = self.first()
        with sqlite3.connect(self.root/'scheduler.sqlite') as c:
            for name, in c.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='scheduler_formal_results'").fetchall():c.execute('DROP TRIGGER '+name)
            c.execute("UPDATE scheduler_formal_results SET result_envelope_hash=?",('f'*64,))
        with self.assertRaises(Exception):
            model.call(purpose='plan',request_id='same-state',prompt='same synthetic state',mission=self.mission)
        self.assertEqual(len(adapter.served),1)

    def planner_fixture(self, adapter=None, *, daily_cost=None, call_budget=None, policy=None):
        from tests.p9a_fixtures import bootstrap_method_authorities, mission_params
        from dalton_core.coverage_mission import CoverageMissionAuthority
        from dalton_core.research_planner_cli import build_state
        store=DaltonStore(str(self.root/'core.sqlite'))
        authority=CoverageMissionAuthority(store)
        params=mission_params(bootstrap_method_authorities(store))
        if daily_cost is not None:
            params['budget']={**params['budget'],'max_daily_cost_usd':daily_cost}
        ref=params.pop('mission_ref'); self.mission=authority.create_mission(ref,**params)
        state=build_state(store,authority,self.mission,plans_dir=self.root/'discovery-plans',
                          as_of=datetime.now(timezone.utc).isoformat())
        store.close()
        adapter=adapter or PlanAdapter()
        config=self._model(adapter,policy_version_ref=policy or self.chain_policy).config
        if call_budget is not None:
            config['purpose_call_budgets']={'plan':call_budget}
        model=CockpitModel(config,scheduler_db=str(self.root/'scheduler.sqlite'),
            adapter_factory=lambda _:adapter,clock=lambda:datetime.now(timezone.utc),
            max_input_tokens=80000,max_output_tokens=4000,max_cost_usd=1.5,timeout_seconds=300)
        config_path=self.root/'research-planner-model-config.json'
        config_path.write_text(json.dumps(config));config_path.chmod(0o600)
        return state,model,adapter,config_path

    def test_real_day_pool_refusal_is_transient_not_a_paid_second_failure(self):
        from dalton_core.research_planner_cli import run_planner
        # The first admission fits the granted pool; its actual overrun is
        # fully booked, exhausting the pool before the fallback admission.
        state,model,adapter,config=self.planner_fixture(PlanAdapter(usd=5.0),daily_cost=5.0)
        kwargs=dict(state_dir=self.root,model_config_path=config,scheduler_db=self.root/'scheduler.sqlite',
                    plans_dir=self.root/'discovery-plans',summary_dir=self.root/'out')
        with patch('dalton_core.research_planner_cli.build_state',return_value=state), \
             patch('dalton_core.research_planner_cli.CockpitModel',return_value=model):
            first=run_planner(**kwargs)
            self.assertEqual(first['plan_status'],'model_unavailable')
            second=run_planner(**kwargs)
            self.assertEqual(second['plan_status'],'skipped:pool_exhausted')
            self.assertNotEqual(second['plan_status'],'output_recovery_failed')
            self.assertEqual(len(adapter.served),1)
            third=run_planner(**kwargs)
            self.assertEqual(third['held_reason'],'transient_backoff')
            old_child=second['paid_output_recovery']['successor_work_ref']
            # Later reads retain this exact no-send child even when the paid
            # parent hold has expired. They neither become terminal nor invent
            # a fresh generation because as_of changed.
            later=copy.deepcopy(state)
            future=datetime.now(timezone.utc)+timedelta(minutes=125)
            later['as_of']=future.isoformat()
            with patch('dalton_core.research_planner_cli.build_state',return_value=later), \
                 patch('dalton_core.research_planner_cli.datetime',wraps=datetime) as clock:
                clock.now.return_value=future
                fourth=run_planner(**kwargs)
            self.assertEqual(fourth['paid_output_recovery']['successor_work_ref'],old_child)
            self.assertNotEqual(fourth['plan_status'],'output_recovery_failed')
            self.assertEqual(fourth['model_failure_trace']['work_order_ref'],old_child)
        with sqlite3.connect(self.root/'scheduler.sqlite') as c:
            errors=[json.loads(r[0]).get('error',{}).get('code') for r in c.execute('SELECT result_envelope_json FROM scheduler_formal_results')]
            self.assertEqual(c.execute('SELECT COUNT(*) FROM scheduler_work_orders').fetchone()[0],2)
        self.assertIn('POOL_EXHAUSTED',errors)
        self.assertEqual(len(adapter.served),1)

    def test_actual_route_refusal_can_follow_existing_route_admission_recovery(self):
        from dalton_core.research_planner_cli import run_planner
        state,model,adapter,config=self.planner_fixture(policy=self.pinned_policy)
        kwargs=dict(state_dir=self.root,model_config_path=config,scheduler_db=self.root/'scheduler.sqlite',
                    plans_dir=self.root/'discovery-plans',summary_dir=self.root/'out')
        with patch('dalton_core.research_planner_cli.build_state',return_value=state), \
             patch('dalton_core.research_planner_cli.CockpitModel',return_value=model):
            first=run_planner(**kwargs)
            self.assertEqual(first['plan_status'],'model_unavailable')
            second=run_planner(**kwargs)
            self.assertNotEqual(second['plan_status'],'output_recovery_failed')
            self.assertEqual(len(adapter.served),1)
        with sqlite3.connect(self.root/'scheduler.sqlite') as c:
            errors=[json.loads(r[0]).get('error',{}).get('code') for r in c.execute('SELECT result_envelope_json FROM scheduler_formal_results')]
        self.assertIn('MODEL_ROUTE_REJECTED',errors)
        # A legitimate owner policy change uses the pre-existing admission
        # recovery of this exact child, not another paid-output generation.
        with ModelRouter(self.router_db) as router:
            old=router.get_policy(self.chain_policy)
            revised={k:copy.deepcopy(v) for k,v in old.items() if k!='content_hash'}
            revised['version']=old['version']+1
            revised['prior_version_ref']=old['policy_version_ref']
            revised['policy_version_ref']=old['policy_version_ref']+'-owner-corrected'
            revised['created_at']=router._now().isoformat()
            revised['fallback_chains']['tiers']['brain']=['profile:claude-fable-5-1']
            router.register_policy(revised)
        model.config['routing_policy_ref']=revised['policy_version_ref']
        from dalton_core.research_planner import build_prompt
        answer=model.call(purpose='plan',request_id=state['content_hash'][:32],
                          prompt=build_prompt(state),mission=self.mission)
        self.assertIn('Synthetic fixture',answer['text'])
        self.assertEqual(adapter.served,['profile:gpt-6-astra','profile:claude-fable-5-1'])

    def test_normal_child_bypasses_same_state_backoff_only_with_strong_paid_proof(self):
        from dalton_core.research_planner_cli import run_planner
        state,model,adapter,config=self.planner_fixture()
        kwargs=dict(state_dir=self.root,model_config_path=config,scheduler_db=self.root/'scheduler.sqlite',
                    plans_dir=self.root/'discovery-plans',summary_dir=self.root/'out')
        with patch('dalton_core.research_planner_cli.build_state',return_value=state), \
             patch('dalton_core.research_planner_cli.CockpitModel',return_value=model):
            first=run_planner(**kwargs)
            self.assertEqual(first['plan_status'],'model_unavailable')
            reread=copy.deepcopy(state)
            reread['as_of']=(datetime.now(timezone.utc)+timedelta(seconds=2)).isoformat()
            with patch('dalton_core.research_planner_cli.build_state',return_value=reread):
                second=run_planner(**kwargs)
            self.assertEqual(second['plan_status'],'fresh')
            self.assertIn('paid_output_recovery',second)
            third=run_planner(**kwargs)
            self.assertEqual(third['plan_status'],'unchanged')
        self.assertEqual(adapter.served,['profile:gpt-6-astra','profile:claude-fable-5-1'])
        with sqlite3.connect(self.root/'core.sqlite') as c:
            self.assertEqual(c.execute('SELECT COUNT(*) FROM coverage_mission_research_plans').fetchone()[0],1)

    def test_outer_idle_gate_naturally_wakes_once_then_existing_child_owns_recovery(self):
        from dalton_core.research_planner_cli import run_planner
        from dalton_core.research_planner_launcher import ResearchPlannerCoordinator
        from tests.test_research_planner_launcher import FakeLauncher
        state,model,adapter,config=self.planner_fixture()
        with patch('dalton_core.research_planner_cli.build_state',return_value=state), \
             patch('dalton_core.research_planner_cli.CockpitModel',return_value=model):
            failed=run_planner(state_dir=self.root,model_config_path=config,
                scheduler_db=self.root/'scheduler.sqlite',plans_dir=self.root/'discovery-plans',summary_dir=self.root/'out')
        store=DaltonStore(str(self.root/'core.sqlite'));self.addCleanup(store.close)
        tickets=self.root/'research-plans';tickets.mkdir()
        launcher=FakeLauncher(tickets)
        launcher.state_dir,launcher.model_config_path,launcher.scheduler_db=self.root,config,self.root/'scheduler.sqlite'
        launcher.ticket={'id':'research-plan:'+'0'*24,'status':'succeeded','summary':failed}
        coordinator=ResearchPlannerCoordinator(store=store,launcher=launcher,clock=lambda:datetime.now(timezone.utc))
        (tickets/'latest.json').write_text(json.dumps({'ticket_ref':launcher.ticket['id'],
            'idle_at':datetime.now(timezone.utc).isoformat(),'idle_signature':coordinator._signature()}))
        self.assertEqual(coordinator.dispatch_once()['status'],'launched')
        self.assertEqual(coordinator.dispatch_once()['status'],'busy')
        self.assertEqual(launcher.starts,1)

        # Once the exact successor is enqueued/settled, stale failure display
        # cannot force another idle bypass, even before a new plan is stored.
        from dalton_core.research_planner import build_prompt
        model.call(purpose='plan',request_id=state['content_hash'][:32],prompt=build_prompt(state),mission=self.mission)
        launcher.finish(failed)
        self.assertEqual(coordinator.dispatch_once()['status'],'held')
        self.assertEqual(launcher.starts,1)

    def test_cli_changed_as_of_and_failed_second_model_remain_two_paid_calls_over_later_ticks(self):
        from dalton_core.research_planner_cli import run_planner
        state,model,adapter,config=self.planner_fixture(BothOverrun())
        kwargs=dict(state_dir=self.root,model_config_path=config,scheduler_db=self.root/'scheduler.sqlite',
                    plans_dir=self.root/'discovery-plans',summary_dir=self.root/'out')
        with patch('dalton_core.research_planner_cli.build_state',return_value=state), \
             patch('dalton_core.research_planner_cli.CockpitModel',return_value=model):
            first=run_planner(**kwargs)
            self.assertEqual(first['plan_status'],'model_unavailable')
            later=copy.deepcopy(state);later['as_of']=datetime.now(timezone.utc).isoformat()
            with patch('dalton_core.research_planner_cli.build_state',return_value=later):
                second=run_planner(**kwargs)
            self.assertEqual(second['plan_status'],'output_recovery_failed')
            self.assertNotIn('_parent_prompt',second['paid_output_recovery'])
            with sqlite3.connect(self.root/'scheduler.sqlite') as c:
                old=c.execute('SELECT * FROM scheduler_formal_results ORDER BY work_order_id').fetchall()
            for minutes in (5,35,125):
                later['as_of']=(datetime.now(timezone.utc)+timedelta(minutes=minutes)).isoformat()
                with patch('dalton_core.research_planner_cli.build_state',return_value=later), \
                     patch('dalton_core.research_planner_cli.datetime',wraps=datetime) as clock:
                    clock.now.return_value=datetime.now(timezone.utc)+timedelta(minutes=minutes)
                    held=run_planner(**kwargs)
                self.assertEqual(held['held_reason'],'terminal_for_this_state')
            with sqlite3.connect(self.root/'scheduler.sqlite') as c:
                self.assertEqual(c.execute('SELECT * FROM scheduler_formal_results ORDER BY work_order_id').fetchall(),old)
        self.assertEqual(adapter.served,['profile:gpt-6-astra','profile:claude-fable-5-1'])
