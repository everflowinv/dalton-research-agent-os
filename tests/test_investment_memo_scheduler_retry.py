"""Finished attempts resume through the real owner, retaining unknown fees."""
import sqlite3
import unittest
from unittest.mock import patch

from dalton_core.cockpit_model import CockpitModelError
from dalton_core.scheduler import Scheduler
from dalton_core.investment_memo_scheduler_retry import prove_ready_retry
from tests import test_investment_memo_macro_accounting as fixture
from tests import test_investment_memo_timeout_recovery as transport


class MemoSchedulerRetryTests(unittest.TestCase):
    setUp = fixture.MemoMacroAccountingTests.setUp
    kw = fixture.MemoMacroAccountingTests.kw
    wrapper = fixture.MemoMacroAccountingTests.wrapper
    fees = fixture.MemoMacroAccountingTests.fees

    def abandon(self, index=0):
        wrapper = self.wrapper(run=112_000_000)
        # The genuine Cockpit lease guard durably completes the failed
        # operation. No adapter call/admission was observed; its envelope
        # deliberately says unknown, so that ceiling must not be cleared.
        with patch('dalton_core.cockpit_model._admit_through_lock',
                   side_effect=sqlite3.OperationalError('fixture database is locked')):
            with self.assertRaises(sqlite3.OperationalError):
                self.model.call(**self.kw(index))
        with Scheduler(self.model.scheduler_db, clock=self.clock) as scheduler:
            row = scheduler.connection.execute('SELECT work_order_id FROM scheduler_work_orders ORDER BY rowid DESC LIMIT 1').fetchone()
            ref = row[0]
            proof = prove_ready_retry(scheduler.connection, ref, mission=self.mission, now=self.clock())
            original = [tuple(r) for r in scheduler.connection.execute('SELECT * FROM scheduler_attempt_events WHERE work_order_id=? ORDER BY event_seq',(ref,))]
        return wrapper, ref, proof, original

    def test_two_companies_real_cockpit_retry_same_work_preserves_unknown_and_restarts_free(self):
        for company in ('company:one', 'company:other'):
            self.company = company
            self.prompts = [transport.MemoTimeoutRecoveryTests.kw(self,i,company)['prompt'] for i in range(4)]
            _, ref, proof, original = self.abandon()
            old_fees = self.fees(); calls = len(self.adapter.served)
            resumed = self.wrapper(run=112_000_000)
            answer = resumed.call(**self.kw(0))
            self.assertEqual(answer['work_order_ref'], ref)
            self.assertEqual(len(self.adapter.served), calls+1)
            with Scheduler(self.model.scheduler_db) as scheduler:
                authority=scheduler.work_order_authority(ref)
                self.assertEqual(authority['work_order_hash'],proof['work_order_hash'])
                self.assertEqual(scheduler.formal_result(ref)['attempt_number'],2)
                self.assertEqual([tuple(r) for r in scheduler.connection.execute('SELECT * FROM scheduler_attempt_events WHERE work_order_id=? ORDER BY event_seq LIMIT 4',(ref,))],original)
            bill = next(b for b in resumed.macro.last['bills'] if b['work_ref']==ref)
            self.assertTrue(bill['unknown_fee_retained'])
            self.assertGreaterEqual(bill['accounted_micros'],proof['accounted_micros'])
            self.assertEqual(bill['retained_retry_unknown_micros'],proof['accounted_micros'])
            self.assertEqual(self.fees()[0][:len(old_fees[0])],old_fees[0])
            restarted = self.wrapper(run=112_000_000)
            self.assertEqual(restarted.call(**self.kw(0))['cost_micros'],0)
            self.assertEqual(restarted.accounted_micros,resumed.accounted_micros)
            self.assertEqual(len(self.adapter.served),calls+1)
            self.assertIsNone(restarted.macro.extra)

    def test_future_groups_and_verifier_reservations_can_refuse_without_spending(self):
        _,ref,proof,_=self.abandon()
        original=self.fees()
        resumed=self.wrapper(run=56_000_000)
        with self.assertRaisesRegex(CockpitModelError,'run ceiling'):resumed.call(**self.kw(0))
        self.assertEqual(self.fees(),original);self.assertEqual(self.adapter.served,[])
        self.assertEqual(resumed.macro.last['ready_retry_work_refs'],[ref])
        self.assertEqual(resumed.macro.last['accounted_micros'],proof['accounted_micros'])
        self.assertFalse(resumed.macro.last['cost_complete'])

    def test_different_group_cannot_use_retry_room_or_reset_pending_work(self):
        _,ref,_,_=self.abandon()
        original=self.fees()
        with self.assertRaisesRegex(CockpitModelError,'original registered request'):
            self.wrapper(run=112_000_000).call(**self.kw(1))
        self.assertEqual(self.fees(),original);self.assertEqual(self.adapter.served,[])
        with Scheduler(self.model.scheduler_db) as scheduler:
            self.assertEqual(scheduler.status(ref)['attempt_number'],2)

    def _changed_configuration(self, change):
        _,ref,_,_=self.abandon()
        original=self.fees()
        if change=='budget':self.model.config['purpose_call_budgets']['investment_memo']['max_output_tokens']=200000
        else:self.model.config['provider_retry']['max_same_profile_retries']+=1
        with self.assertRaisesRegex(CockpitModelError,'exact existing Work'):
            self.wrapper(run=112_000_000).call(**self.kw(0))
        self.assertEqual(self.fees(),original);self.assertEqual(self.adapter.served,[])
        with Scheduler(self.model.scheduler_db) as scheduler:
            self.assertEqual(scheduler.connection.execute('SELECT count(*) FROM scheduler_work_orders').fetchone()[0],1)

    def test_changed_effective_budget_cannot_create_another_work(self):
        self._changed_configuration('budget')

    def test_changed_provider_policy_cannot_create_another_work(self):
        self._changed_configuration('policy')

    def test_normal_daily_admission_refusal_preserves_previous_unknown_ceiling(self):
        from dalton_core.thesis_impact_budget import ThesisImpactBudgetStore
        _,ref,proof,_=self.abandon()
        with ThesisImpactBudgetStore(self.root/'budget.sqlite') as budget:
            budget.register_policy(policy_version_id='strict-day',day_cap_micros=1)
        self.model.config['budget_policy_ref']='strict-day'
        resumed=self.wrapper(run=112_000_000)
        with self.assertRaises(CockpitModelError):resumed.call(**self.kw(0))
        self.assertEqual(self.adapter.served,[]);self.assertEqual(self.fees(),([],[]))
        self.assertGreaterEqual(resumed.accounted_micros,proof['accounted_micros'])
        with Scheduler(self.model.scheduler_db) as scheduler:
            self.assertEqual(scheduler.formal_result(ref)['attempt_number'],2)

    def test_rehashed_prompt_or_unknown_constraint_cannot_reuse_registration(self):
        _,ref,_,_=self.abandon()
        kwargs={**self.kw(0),'prompt':self.kw(0)['prompt']+' changed'}
        with self.assertRaises(CockpitModelError):self.wrapper(run=112_000_000).call(**kwargs)
        with self.assertRaises(CockpitModelError):
            self.model.call(**self.kw(0),_existing_memo_retry={'work_order_ref':ref,'work_order_hash':'0'*64,'grant':'fake'})
        self.assertEqual(self.adapter.served,[])

    def test_ready_lease_or_foreign_mission_are_not_finished_retry_authority(self):
        _,ref,_,_=self.abandon()
        with Scheduler(self.model.scheduler_db,clock=self.clock) as scheduler:
            with self.assertRaises(ValueError):
                prove_ready_retry(scheduler.connection,ref,mission={**self.mission,'id':'mission:foreign'},now=self.clock())
            scheduler.claim('owner:still-working',work_order_id=ref,lease_seconds=30)
            with self.assertRaises(ValueError):
                prove_ready_retry(scheduler.connection,ref,mission=self.mission,now=self.clock())
        original=self.fees()
        with self.assertRaisesRegex(CockpitModelError,'in flight'):
            self.wrapper(run=112_000_000).call(**self.kw(0))
        self.assertEqual(self.fees(),original);self.assertEqual(self.adapter.served,[])

    def test_completed_result_hash_tamper_refuses_without_new_call(self):
        _,ref,_,_=self.abandon()
        with Scheduler(self.model.scheduler_db) as scheduler:
            for r in scheduler.connection.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='scheduler_result_envelopes'").fetchall():
                scheduler.connection.execute('DROP TRIGGER '+r[0])
            scheduler.connection.execute('UPDATE scheduler_result_envelopes SET result_envelope_hash=? WHERE work_order_id=?',('0'*64,ref))
            with self.assertRaisesRegex(ValueError,'result differs'):
                prove_ready_retry(scheduler.connection,ref,mission=self.mission,now=self.clock())
        with self.assertRaises(CockpitModelError):self.wrapper(run=112_000_000).call(**self.kw(0))
        self.assertEqual(self.adapter.served,[])

    def test_same_work_constraint_cannot_authorize_an_unregistered_work(self):
        with self.assertRaisesRegex(CockpitModelError,'exact existing Work'):
            self.model.call(**self.kw(0),_existing_memo_retry={'work_order_ref':'work:missing','work_order_hash':'0'*64})
        self.assertEqual(self.adapter.served,[]);self.assertEqual(self.fees(),([],[]))

    def test_internal_provider_retry_cannot_spend_future_group_or_verifier_room(self):
        from tests import test_cockpit_model_fallback as models
        _,ref,_,_=self.abandon()
        adapter=models.ReturnedProviderSequenceAdapter(['RATE_LIMITED','PROVIDER_INTERNAL_ERROR'])
        self.model.adapter_factory=lambda router:adapter
        # This policy was already frozen into the Work. Keep its identity:
        # the fixture's original backoff is 2 seconds, which is made due by
        # advancing the deterministic clock at the normal backoff boundary.
        with patch('time.sleep',side_effect=lambda seconds:self.clock.advance(seconds)):
            # Old unknown14 + future56 + new attempt14 = 84. Another attempt
            # must be refused even if the returned first failure cost is low.
            with self.assertRaisesRegex(CockpitModelError,'retained run room'):
                self.wrapper(run=84_000_000).call(**self.kw(0))
        self.assertEqual(len(adapter.served),1)
        with Scheduler(self.model.scheduler_db) as scheduler:
            self.assertIsNone(scheduler.formal_result(ref))
            self.assertEqual(scheduler.status(ref)['attempt_number'],3)
            self.assertEqual(scheduler.status(ref)['state'],'ready')



class MemoMaintenanceSchedulerRetryTests(unittest.TestCase):
    """The actual fixed owner permission, not only a normal group, resumes."""
    from tests import test_investment_memo_group_maintenance as maintenance_fixture
    setUp = maintenance_fixture.MemoGroupMaintenanceTests.setUp
    make_contexts = maintenance_fixture.MemoGroupMaintenanceTests.make_contexts
    kw = maintenance_fixture.MemoGroupMaintenanceTests.kw
    outcome = maintenance_fixture.MemoGroupMaintenanceTests.outcome
    old_repair = maintenance_fixture.MemoGroupMaintenanceTests.old_repair
    seeded = maintenance_fixture.MemoGroupMaintenanceTests.seeded
    fees = maintenance_fixture.MemoGroupMaintenanceTests.fees

    def wrapper(self):
        return self.maintenance_fixture.MemoGroupMaintenanceTests.wrapper(self,run=112_000_000)

    def test_fixed_maintenance_original_permission_attempt_and_paid_parent_are_retained(self):
        from dalton_core.investment_memo_group_maintenance import TABLE
        from dalton_core.investment_memo_macro_accounting import EXTRA
        wrapper,outcome=self.seeded()
        body=wrapper.prepare_maintenance(context=self.contexts[1],outcome=outcome,remaining_groups=2)
        self.assertIsNotNone(body)
        wrapper.begin_stage(max_units=4,allow_technical=False,maintenance=body)
        kwargs={**self.kw(1),'prompt':body['prompt'],'request_id':body['entry']['request_id']}
        with patch('dalton_core.cockpit_model._admit_through_lock',side_effect=sqlite3.OperationalError('fixture lock')):
            with self.assertRaises(sqlite3.OperationalError):wrapper.call(**kwargs)
        original=self.fees();calls=len(self.adapter.served)
        with Scheduler(self.model.scheduler_db) as scheduler:
            permission=tuple(scheduler.connection.execute(f'SELECT * FROM {TABLE}').fetchone())
            extra=tuple(scheduler.connection.execute(f'SELECT * FROM {EXTRA}').fetchone())
            ref=scheduler.connection.execute('SELECT work_order_id FROM scheduler_work_orders ORDER BY rowid DESC LIMIT 1').fetchone()[0]
            proof=prove_ready_retry(scheduler.connection,ref,mission=self.mission,now=self.clock())
        resumed=self.wrapper()
        current=self.outcome(resumed,1)
        self.assertEqual(current['status'],'drafted')
        self.assertEqual(current['model']['work_order_ref'],ref)
        self.assertEqual(len(self.adapter.served),calls+1)
        with Scheduler(self.model.scheduler_db) as scheduler:
            self.assertEqual(tuple(scheduler.connection.execute(f'SELECT * FROM {TABLE}').fetchone()),permission)
            self.assertEqual(tuple(scheduler.connection.execute(f'SELECT * FROM {EXTRA}').fetchone()),extra)
            self.assertEqual(scheduler.formal_result(ref)['attempt_number'],2)
        self.assertEqual(self.fees()[0][:len(original[0])],original[0])
        self.assertEqual(self.fees()[1][:len(original[1])],original[1])
        self.assertGreaterEqual(resumed.accounted_micros,proof['accounted_micros'])
        self.assertEqual(self.outcome(self.wrapper(),1)['status'],'drafted')
        self.assertEqual(len(self.adapter.served),calls+1)


    def test_maintenance_internal_retry_retains_floor_even_when_later_groups_are_free(self):
        from tests import test_cockpit_model_fallback as models
        wrapper,outcome=self.seeded()
        self.assertEqual(self.outcome(wrapper,2)['status'],'drafted')
        self.assertEqual(self.outcome(wrapper,3)['status'],'drafted')
        body=wrapper.prepare_maintenance(context=self.contexts[1],outcome=outcome,remaining_groups=2)
        self.assertIsNotNone(body)
        wrapper.begin_stage(max_units=4,allow_technical=False,maintenance=body)
        kwargs={**self.kw(1),'prompt':body['prompt'],'request_id':body['entry']['request_id']}
        with patch('dalton_core.cockpit_model._admit_through_lock',side_effect=sqlite3.OperationalError('fixture lock')):
            with self.assertRaises(sqlite3.OperationalError):wrapper.call(**kwargs)
        original=self.fees()
        adapter=models.ReturnedProviderSequenceAdapter(['RATE_LIMITED','PROVIDER_INTERNAL_ERROR'])
        self.model.adapter_factory=lambda router:adapter
        resumed=self.maintenance_fixture.MemoGroupMaintenanceTests.wrapper(self,run=84_000_000)
        with patch('time.sleep',side_effect=lambda seconds:self.clock.advance(seconds)):
            with self.assertRaisesRegex(CockpitModelError,'retained run room'):
                resumed.call(**self.kw(1))
        self.assertEqual(len(adapter.served),1)
        self.assertEqual(self.fees()[0][:len(original[0])],original[0])
        self.assertEqual(self.fees()[1][:len(original[1])],original[1])
