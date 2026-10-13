"""Exact memo stages share one extra-call slot and preserve paid checkpoints."""
import json
import unittest
from unittest.mock import patch
from tests import test_cockpit_model_fallback as fixture
from dalton_core.cockpit_model import CockpitModelError
from dalton_core.investment_memo_repair_budget import MemoRepairBudget
from dalton_core.investment_memo_draft import GROUPS
from dalton_core.investment_memo_timeout_recovery import MACRO_TABLE
from dalton_core.scheduler import Scheduler
from dalton_core.thesis_impact_budget import ThesisImpactBudgetStore
from dalton_core.store import content_hash


class MemoTimeoutRecoveryTests(unittest.TestCase):
    def setUp(self):
        fixture.CockpitChainTests.setUp(self)
        self.clock=fixture.MutableClock()
        with ThesisImpactBudgetStore(self.root/'budget.sqlite') as b:
            b.register_policy(policy_version_id='memo-test-day-policy',day_cap_micros=100_000_000)
        self.mission['budget']['max_daily_cost_usd']=100
        self.adapter=fixture.ChainAdapter({})
        self.model=fixture.CockpitChainTests._model(self,self.adapter,
            policy_version_ref=self.chain_policy,clock=self.clock,
            provider_retry={'max_same_profile_retries':1,'retry_backoff_seconds':2})
        self.model.config['budget_policy_ref']='memo-test-day-policy'
        self.model.config['purpose_call_budgets']['investment_memo']={
            'max_input_tokens':120000,'max_output_tokens':500,'max_cost_usd':0.5,'timeout_seconds':180}
        self.patch=patch('dalton_core.cockpit_model._now',side_effect=lambda:self.clock().isoformat())
        self.patch.start();self.addCleanup(self.patch.stop)
        self.company='company:one'

    def kw(self,index,company=None):
        prompt='prefix\nCompany: '+json.dumps({'company_ref':company or self.company})+'\nGroup: '+GROUPS[index][0]+'\nEvidence rows:\n[]\nReturn raw JSON only with the requested shape'
        return dict(purpose='investment_memo',request_id='memo-'+content_hash(prompt)[:24],prompt=prompt,mission=self.mission)

    def wrapper(self,run=70_000_000):
        wrapper=MemoRepairBudget(self.model,run_micros=run,producer_micros=14_000_000,verifier_micros=14_000_000)
        wrapper.begin_stage(max_units=4,allow_technical=True)
        return wrapper

    def fail_third(self,company=None):
        wrapper=self.wrapper(); first=[]
        for index in range(2):first.append(wrapper.call(**self.kw(index,company)))
        self.adapter.script={'profile:gpt-6-astra':{'code':'TIMEOUT','message':'host completion exceeded request timeout'}}
        with self.assertRaises(CockpitModelError):wrapper.call(**self.kw(2,company))
        self.clock.advance(61)
        return first

    def resume(self,company=None,run=70_000_000):
        wrapper=self.wrapper(run)
        for index in range(2):wrapper.call(**self.kw(index,company))
        return wrapper

    def fee_rows(self):
        with ThesisImpactBudgetStore(self.root/'budget.sqlite') as b:
            return [tuple(r) for r in b.connection.execute('SELECT admission_id,record_json FROM thesis_impact_day_admissions ORDER BY rowid')],[tuple(r) for r in b.connection.execute('SELECT settlement_id,record_json FROM thesis_impact_day_settlements ORDER BY rowid')]

    def test_two_companies_resume_paid_groups_without_rebuying_or_refunding_timeout(self):
        for company in ('company:one','company:two'):
            self.adapter.script={};self.fail_third(company);old=self.fee_rows();calls=len(self.adapter.served)
            self.adapter.script={};wrapper=self.resume(company);answer=wrapper.call(**self.kw(2,company))
            self.assertEqual(len(self.adapter.served),calls+1)
            self.assertTrue(answer['timeout_recovery_authorization_ref'].startswith('cockpit-timeout-authorization:'))
            new=self.fee_rows();self.assertEqual(new[0][:len(old[0])],old[0]);self.assertEqual(new[1][:len(old[1])],old[1])
            self.assertGreaterEqual(wrapper.accounted_micros,14_000_000)
            self.assertFalse(wrapper.can_repair(remaining_groups=1))
            wrapper.call(**self.kw(3,company));self.assertEqual(len(self.adapter.served),calls+2)
        with Scheduler(self.model.scheduler_db) as s:
            self.assertEqual(s.connection.execute('SELECT count(*) FROM '+MACRO_TABLE).fetchone()[0],2)

    def test_child_timeout_multiple_restarts_never_create_another_paid_generation(self):
        self.fail_third(); before=len(self.adapter.served)
        for _ in range(3):
            wrapper=self.resume()
            with self.assertRaises(CockpitModelError):wrapper.call(**self.kw(2))
        self.assertEqual(len(self.adapter.served),before+1)
        with Scheduler(self.model.scheduler_db) as s:
            self.assertEqual(s.connection.execute('SELECT count(*) FROM '+MACRO_TABLE).fetchone()[0],1)

    def test_insufficient_macro_headroom_does_not_buy_technical_child(self):
        self.fail_third();self.adapter.script={};before=len(self.adapter.served)
        wrapper=self.resume(run=40_000_000)
        with self.assertRaises(CockpitModelError):wrapper.call(**self.kw(2))
        self.assertEqual(len(self.adapter.served),before)

    def test_pending_settlement_never_authorizes_memo_continuation(self):
        wrapper=self.wrapper();wrapper.call(**self.kw(0));wrapper.call(**self.kw(1))
        self.adapter.script={'profile:gpt-6-astra':{'code':'TIMEOUT','message':'timeout'}}
        with patch('dalton_core.cockpit_model._settle_without_losing_the_lease',return_value=None):
            with self.assertRaises(CockpitModelError):wrapper.call(**self.kw(2))
        self.clock.advance(61);self.adapter.script={};before=len(self.adapter.served)
        wrapper=self.resume()
        with self.assertRaises(CockpitModelError):wrapper.call(**self.kw(2))
        self.assertEqual(len(self.adapter.served),before)

    def test_prior_semantic_repair_consumes_the_same_extra_slot(self):
        self.fail_third();self.adapter.script={};before=len(self.adapter.served)
        wrapper=self.resume();wrapper.begin_stage(max_units=4,allow_technical=False)
        with self.assertRaises(CockpitModelError):wrapper.call(**self.kw(2))
        self.assertEqual(len(self.adapter.served),before)

    def test_second_group_timeout_cannot_claim_same_macro_slot(self):
        self.fail_third();self.adapter.script={};wrapper=self.resume();wrapper.call(**self.kw(2))
        self.adapter.script={'profile:gpt-6-astra':{'code':'TIMEOUT','message':'timeout'}}
        with self.assertRaises(CockpitModelError):wrapper.call(**self.kw(3))
        self.clock.advance(61);before=len(self.adapter.served);self.adapter.script={}
        wrapper=self.resume();wrapper.call(**self.kw(2))
        with self.assertRaises(CockpitModelError):wrapper.call(**self.kw(3))
        self.assertEqual(len(self.adapter.served),before)

    def test_ordinary_direct_memo_call_cannot_auto_recover_outside_macro_guard(self):
        self.fail_third();before=len(self.adapter.served);self.adapter.script={}
        with self.assertRaises(CockpitModelError):self.model.call(**self.kw(2))
        self.assertEqual(len(self.adapter.served),before)

    def test_pause_arriving_at_technical_boundary_prevents_new_dispatch(self):
        self.fail_third();before=len(self.adapter.served);self.adapter.script={};wrapper=self.resume()
        checkpoint=unittest.mock.Mock();checkpoint.checkpoint.side_effect=RuntimeError('pause boundary')
        wrapper.begin_stage(max_units=4,allow_technical=True,quiesce=checkpoint)
        with self.assertRaisesRegex(RuntimeError,'pause boundary'):wrapper.call(**self.kw(2))
        self.assertEqual(len(self.adapter.served),before)

    def test_normal_daily_call_cap_blocks_technical_child_without_refunding_parent(self):
        self.mission['budget']['max_daily_paid_calls']=3
        self.fail_third();before=self.fee_rows();calls=len(self.adapter.served);self.adapter.script={}
        wrapper=self.resume()
        with self.assertRaises(CockpitModelError):wrapper.call(**self.kw(2))
        self.assertEqual(len(self.adapter.served),calls)
        self.assertEqual(self.fee_rows(),before)

    def test_non_timeout_failure_does_not_receive_technical_slot(self):
        wrapper=self.wrapper();wrapper.call(**self.kw(0));wrapper.call(**self.kw(1))
        self.adapter.script={'profile:gpt-6-astra':{'code':'PROVIDER_ERROR','message':'unknown'}}
        with self.assertRaises(CockpitModelError):wrapper.call(**self.kw(2))
        self.clock.advance(61);calls=len(self.adapter.served);self.adapter.script={}
        wrapper=self.resume()
        with self.assertRaises(CockpitModelError):wrapper.call(**self.kw(2))
        self.assertEqual(len(self.adapter.served),calls)

    def test_config_change_before_authorization_keeps_old_unknown_macro_bill(self):
        self.fail_third();old=self.fee_rows()
        self.model.config['purpose_call_budgets']['investment_memo']['max_output_tokens']=600
        wrapper=self.resume()
        with self.assertRaises(CockpitModelError):wrapper.call(**self.kw(2))
        self.clock.advance(61);before=len(self.adapter.served);self.adapter.script={}
        wrapper=self.resume()
        with self.assertRaises(CockpitModelError):wrapper.call(**self.kw(2))
        self.assertEqual(len(self.adapter.served),before)
        self.assertGreater(wrapper.accounted_micros,28_000_000)
        self.assertEqual(self.fee_rows()[1][:len(old[1])],old[1])

    def test_completed_formal_tampering_cannot_buy_a_child(self):
        first=self.fail_third();before=len(self.adapter.served);self.adapter.script={}
        with Scheduler(self.model.scheduler_db) as s:
            for row in s.connection.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='scheduler_formal_results'").fetchall():
                s.connection.execute('DROP TRIGGER '+row[0])
            s.connection.execute('UPDATE scheduler_formal_results SET content_hash=? WHERE work_order_id=?',('0'*64,first[0]['work_order_ref']))
        wrapper=self.wrapper()
        with self.assertRaisesRegex(ValueError,'completed formal hash differs'):
            wrapper.call(**self.kw(0));wrapper.call(**self.kw(1));wrapper.call(**self.kw(2))
        self.assertEqual(len(self.adapter.served),before)

    def test_concurrent_resumes_share_one_macro_slot_and_one_physical_child(self):
        from concurrent.futures import ThreadPoolExecutor
        self.fail_third();self.adapter.script={};before=len(self.adapter.served)
        def resume():
            try:return self.resume().call(**self.kw(2))
            except CockpitModelError:return None
        # Keep normal long-lived authorities open, as the live writer does.
        with Scheduler(self.model.scheduler_db), ThesisImpactBudgetStore(self.root/'budget.sqlite'):
            with ThreadPoolExecutor(max_workers=2) as pool:
                list(pool.map(lambda _:resume(),range(2)))
        self.assertEqual(len(self.adapter.served),before+1)
        with Scheduler(self.model.scheduler_db) as s:
            self.assertEqual(s.connection.execute('SELECT count(*) FROM '+MACRO_TABLE).fetchone()[0],1)

    def test_rehashed_scope_cannot_omit_a_matching_historical_bill(self):
        from dalton_core.cockpit_timeout_recovery import TABLE, failure_proof
        from dalton_core.investment_memo_timeout_recovery import verify_scope
        from dalton_core.planner_paid_output_recovery import recovery_readers
        self.fail_third();self.adapter.script={}
        self.resume().call(**self.kw(2))
        with Scheduler(self.model.scheduler_db) as scheduler, recovery_readers(
                self.model.config['model_router_db'],self.model.config['budget_db'],
                now=self.clock()) as (router,budget):
            record=json.loads(scheduler.connection.execute(
                'SELECT record_json FROM '+TABLE).fetchone()[0])
            proof=failure_proof(scheduler.connection,router,budget,
                work_ref=record['proof']['parent_work_ref'],mission=self.mission)
            scope=record['memo_scope']
            self.assertEqual(len(scope['historical_costs']),3)
            scope['historical_costs']=scope['historical_costs'][1:]
            record['content_hash']=content_hash({k:v for k,v in record.items() if k!='content_hash'})
            before=len(self.adapter.served)
            with self.assertRaisesRegex(ValueError,'historical macro bills differ'):
                verify_scope(scheduler.connection,router,budget,scope,proof,mission=self.mission)
            self.assertEqual(len(self.adapter.served),before)
