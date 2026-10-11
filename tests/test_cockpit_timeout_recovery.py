"""Unknown paid timeout keeps its bill and receives one exact normal-budget child."""
import json
import unittest
from unittest.mock import patch
from tests import test_cockpit_model_fallback as fixture
from dalton_core import deep_insight_gate_draft  # registers the normal purpose
from dalton_core.cockpit_model import CockpitModelError
from dalton_core.scheduler import Scheduler
from dalton_core.thesis_impact_budget import ThesisImpactBudgetStore
from dalton_core.model_router import ModelRouter
from dalton_core.cockpit_timeout_recovery import TABLE, MARKER, failure_proof, reserve
from dalton_core.store import content_hash


class CockpitTimeoutRecoveryTests(unittest.TestCase):
    def setUp(self):
        fixture.CockpitChainTests.setUp(self)
        self.clock = fixture.MutableClock()
        self.adapter = fixture.ChainAdapter({'profile:gpt-6-astra':{
            'code':'TIMEOUT','message':'host completion exceeded request timeout'}})
        self.model = fixture.CockpitChainTests._model(self, self.adapter,
            policy_version_ref=self.chain_policy, clock=self.clock,
            provider_retry={'max_same_profile_retries':1,'retry_backoff_seconds':2})
        self.model.config['purpose_call_budgets']['deep_insight_gate'] = {
            'max_input_tokens':120000,'max_output_tokens':500,'max_cost_usd':0.5,'timeout_seconds':180}
        self.patch = patch('dalton_core.cockpit_model._now', side_effect=lambda:self.clock().isoformat())
        self.patch.start(); self.addCleanup(self.patch.stop)
        self.kw = dict(purpose='deep_insight_gate',request_id='one-semantic-group',
                       prompt='exact source-supported questions', mission=self.mission)

    def fail(self):
        with self.assertRaises(CockpitModelError):self.model.call(**self.kw)
        with Scheduler(self.model.scheduler_db) as scheduler:
            rows=scheduler.connection.execute('SELECT work_order_id FROM scheduler_work_orders ORDER BY rowid').fetchall()
            parent=scheduler.work_order_authority(rows[0][0])
            formal=scheduler.formal_result(rows[0][0])
        self.assertEqual(formal['result_envelope']['metadata']['chain_failures'][0]['code'],'TIMEOUT')
        return parent, formal

    def snapshot(self):
        with ThesisImpactBudgetStore(self.root/'budget.sqlite') as budget:
            return [tuple(r) for r in budget.connection.execute(
                'SELECT admission_id,record_json FROM thesis_impact_day_admissions ORDER BY rowid')], [tuple(r) for r in budget.connection.execute(
                'SELECT settlement_id,record_json FROM thesis_impact_day_settlements ORDER BY rowid')]

    def test_single_successor_keeps_unknown_full_fee_and_replays_after_deadline(self):
        parent,formal=self.fail(); old=self.snapshot(); self.clock.advance(61)
        self.adapter.script={}
        result=self.model.call(**self.kw)
        self.assertNotEqual(result['work_order_ref'],parent['work_order']['id'])
        new=self.snapshot();self.assertEqual(new[0][0],old[0][0]);self.assertEqual(new[1][0],old[1][0])
        self.assertEqual(len(new[0]),2);self.assertEqual(len(self.adapter.served),2)
        with Scheduler(self.model.scheduler_db) as scheduler:
            record=json.loads(scheduler.connection.execute('SELECT record_json FROM '+TABLE).fetchone()[0])
            child=scheduler.work_order_authority(result['work_order_ref'])['work_order']
            self.assertEqual(child['metadata']['timeout_recovery'],record)
            self.assertGreater(record['proof']['accounted_micros'],0)
            self.assertEqual(child['budget'],parent['work_order']['budget'])
            self.assertEqual(scheduler.formal_result(parent['work_order']['id']),formal)
        self.clock.advance(172800)
        self.assertTrue(self.model.call(**self.kw)['replayed'])
        self.assertEqual(len(self.adapter.served),2)

    def test_child_timeout_never_generates_third_paid_generation(self):
        self.fail();self.clock.advance(61)
        for _ in range(3):
            with self.assertRaises(CockpitModelError):self.model.call(**self.kw)
        self.clock.advance(86400)
        with self.assertRaises(CockpitModelError):self.model.call(**self.kw)
        self.assertEqual(len(self.adapter.served),2)
        with Scheduler(self.model.scheduler_db) as s:
            self.assertEqual(s.connection.execute('SELECT count(*) FROM scheduler_work_orders').fetchone()[0],2)
            self.assertEqual(s.connection.execute('SELECT count(*) FROM '+TABLE).fetchone()[0],1)

    def test_backoff_and_expired_frozen_policy_refuse_without_more_calls(self):
        self.model.config['provider_retry']['unknown_recovery']={
            'max_fresh_work_orders':2,'retry_backoff_seconds':120,'max_elapsed_seconds':300}
        self.fail()
        with self.assertRaises(CockpitModelError):self.model.call(**self.kw)
        self.clock.advance(301)
        with self.assertRaises(CockpitModelError):self.model.call(**self.kw)
        self.assertEqual(len(self.adapter.served),1)

    def test_frozen_optout_is_not_replaced_by_maintenance_policy(self):
        self.model.config['provider_retry']['unknown_recovery']={
            'max_fresh_work_orders':0,'retry_backoff_seconds':0,'max_elapsed_seconds':300}
        self.fail();self.clock.advance(61)
        with self.assertRaises(CockpitModelError):self.model.call(**self.kw)
        self.assertEqual(len(self.adapter.served),1)

    def test_normal_mission_day_budget_still_refuses_successor(self):
        self.mission['budget']['max_daily_paid_calls']=1
        self.fail();old=self.snapshot();self.clock.advance(61);self.adapter.script={}
        with self.assertRaises(CockpitModelError):self.model.call(**self.kw)
        self.assertEqual(len(self.adapter.served),1)
        self.assertEqual(self.snapshot(),old)

    def test_other_unknown_error_and_success_do_not_qualify(self):
        self.adapter.script={'profile:gpt-6-astra':{'code':'PROVIDER_ERROR','message':'unknown'}}
        with self.assertRaises(CockpitModelError):self.model.call(**self.kw)
        self.clock.advance(61)
        with self.assertRaises(CockpitModelError):self.model.call(**self.kw)
        self.assertEqual(len(self.adapter.served),1)

    def test_parent_formal_tamper_refuses_before_new_dispatch(self):
        parent,formal=self.fail();self.clock.advance(61)
        with Scheduler(self.model.scheduler_db) as s:
            for row in s.connection.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='scheduler_formal_results'").fetchall():
                s.connection.execute('DROP TRIGGER '+row[0])
            s.connection.execute('UPDATE scheduler_formal_results SET content_hash=? WHERE work_order_id=?',
                                 ('0'*64,parent['work_order']['id']))
        with self.assertRaisesRegex(ValueError,'formal authority drifted'):self.model.call(**self.kw)
        self.assertEqual(len(self.adapter.served),1)

    def test_current_user_policy_routes_child_without_replacing_old_route(self):
        parent,formal=self.fail();self.clock.advance(61);self.adapter.script={}
        from dalton_core.research_planner_setup import ensure_planner_policy, credential_slots_for
        with ModelRouter(self.router_db) as router:
            policy=ensure_planner_policy(router,profile_ids=['profile:claude-fable-5-1'],
                now=self.clock(),policy_id='model-routing-policy:current-owner')
            self.current_policy=policy['policy_version_ref']
            self.current_slots=credential_slots_for(router,['profile:claude-fable-5-1'])
        self.model.config['routing_policy_ref']=self.current_policy
        self.model.config['credential_slot_refs']=self.current_slots
        result=self.model.call(**self.kw)
        with ModelRouter(self.router_db,read_only=True) as r:
            old=r.get_decision(formal['result_envelope']['metadata']['route_decision_ref'])
            new=r.get_decision(result['route_decision_ref'])
        self.assertEqual(old['policy_version_ref'],self.chain_policy)
        self.assertEqual(new['policy_version_ref'],self.current_policy)
        self.assertNotEqual(self.adapter.served[0],self.adapter.served[1])
        self.assertGreater(result['prior_unknown_accounted_micros'],0)
        self.assertEqual(result['task_accounted_micros'],result['cost_micros']+result['prior_unknown_accounted_micros'])

    def test_later_epoch_cannot_consume_second_physical_child(self):
        self.fail();self.clock.advance(61)
        with self.assertRaises(CockpitModelError):self.model.call(**self.kw)
        # Changing retry controls must not grant another child under the same
        # semantic certificate. Same budget/same prompt, different Work body.
        self.model.config['provider_retry']['max_same_profile_retries']=0
        with self.assertRaisesRegex(CockpitModelError,'already consumed this exact task'):
            self.model.call(**self.kw)
        self.assertEqual(len(self.adapter.served),2)

    def test_old_timeout_without_policy_uses_new_single_authorization_clock(self):
        self.fail();self.clock.advance(172800);self.adapter.script={}
        result=self.model.call(**self.kw)
        with Scheduler(self.model.scheduler_db) as s:
            rec=json.loads(s.connection.execute('SELECT record_json FROM '+TABLE).fetchone()[0])
        self.assertEqual(rec['created_at'],self.clock().isoformat())
        self.assertEqual(rec['policy_source'],'versioned_maintenance')
        self.assertEqual(len(self.adapter.served),2)


    def test_changed_config_replays_completed_child_for_free(self):
        self.fail();self.clock.advance(61);self.adapter.script={}
        answered=self.model.call(**self.kw)
        self.model.config['provider_retry']['max_same_profile_retries']=0
        replay=self.model.call(**self.kw)
        self.assertTrue(replay['replayed'])
        self.assertEqual(replay['work_order_ref'],answered['work_order_ref'])
        self.assertEqual(len(self.adapter.served),2)

    def test_unknown_pending_original_reservation_is_preserved(self):
        with patch('dalton_core.cockpit_model._settle_without_losing_the_lease',return_value=None):
            parent,formal=self.fail()
        self.clock.advance(61);self.adapter.script={}
        result=self.model.call(**self.kw)
        with ThesisImpactBudgetStore(self.root/'budget.sqlite') as b:
            old=b.admission(work_order_ref=parent['work_order']['id'],attempt_number=1,phase='assessment')
        self.assertIsNone(old['settlement'])
        self.assertEqual(result['prior_unknown_accounted_micros'],old['admission']['reserved_micros'])
        self.assertEqual(len(self.adapter.served),2)

    def test_concurrent_ticks_share_one_authorization_and_one_child(self):
        from concurrent.futures import ThreadPoolExecutor
        self.fail();self.clock.advance(61);self.adapter.script={}
        def tick():
            try:return self.model.call(**self.kw)
            except CockpitModelError:return None
        with ThreadPoolExecutor(max_workers=2) as pool:out=list(pool.map(lambda _:tick(),range(2)))
        self.assertTrue(any(v is not None for v in out))
        self.assertEqual(len(self.adapter.served),2)
        with Scheduler(self.model.scheduler_db) as s:
            self.assertEqual(s.connection.execute('SELECT count(*) FROM '+TABLE).fetchone()[0],1)
            self.assertEqual(s.connection.execute('SELECT count(*) FROM scheduler_work_orders').fetchone()[0],2)

    def test_frozen_policy_can_use_one_successor_within_original_elapsed(self):
        self.model.config['provider_retry']['unknown_recovery']={
            'max_fresh_work_orders':2,'retry_backoff_seconds':120,'max_elapsed_seconds':300}
        self.fail();self.clock.advance(121);self.adapter.script={}
        self.model.call(**self.kw)
        with Scheduler(self.model.scheduler_db) as s:
            rec=json.loads(s.connection.execute('SELECT record_json FROM '+TABLE).fetchone()[0])
        self.assertEqual(rec['policy_source'],'frozen_unknown_recovery')
        self.assertEqual(rec['maximum_successors'],1)
        self.assertEqual(len(self.adapter.served),2)
