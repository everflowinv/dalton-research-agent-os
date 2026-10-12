"""Real Scheduler attempts cannot spend beyond a frozen deepgate run."""
import unittest
from dataclasses import replace
from tests import test_deep_insight_gate_accounting as fixture
from tests.test_cockpit_model_fallback import ReturnedProviderSequenceAdapter
from dalton_core.cockpit_model import CockpitModelError


class DeepGateAdmissionGuardTests(unittest.TestCase):
    def setUp(self):
        fixture.DeepGateAccountingTests.setUp(self)
        self.model.config['provider_retry']['retry_backoff_seconds']=0
        self.account=fixture.DeepGateAccountingTests.accounting(self)

    def install(self,cap):
        def check(*,work,attempt_number,reserved_micros,mission_binding):
            if (mission_binding['mission_version_ref']!=self.mission['id']
                    or mission_binding['mission_version_hash']!=self.mission['content_hash']):
                raise ValueError('different mission')
            self.account.before_admission(work=work,attempt_number=attempt_number,
                reserved_micros=reserved_micros,mission=self.mission,run_micros=cap)
        self.model._run_cost_admission_guard=check

    def metered(self):
        class Metered(ReturnedProviderSequenceAdapter):
            def execute(self,*args):
                invocation,result=super().execute(*args)
                return replace(invocation,usage={'raw_provider_telemetry':{
                    'cost':{'available':True,'usd':0.4}}}),result
        self.adapter=Metered(['RATE_LIMITED','PROVIDER_INTERNAL_ERROR'])
        self.model.adapter_factory=lambda _:self.adapter

    def test_exact_active_first_attempt_runs_but_second_paid_attempt_is_refused(self):
        self.metered();self.install(700000)
        with self.assertRaisesRegex(CockpitModelError,'original run budget'):
            self.model.call(**fixture.DeepGateAccountingTests.kw(self,'industry'))
        self.assertEqual(len(self.adapter.served),1)
        proof=self.account.snapshot()[2]
        self.assertEqual(proof['accounted_micros'],400000)
        self.assertEqual(proof['current_run_cost']['settled_micros'],400000)
        self.assertEqual(proof['unknown_work_refs'],[])
        self.assertEqual(len(proof['bills'][0]['fees']),1)
        fees=fixture.fixture.MemoTimeoutRecoveryTests.fee_rows(self)
        with self.assertRaises(CockpitModelError):
            self.model.call(**fixture.DeepGateAccountingTests.kw(self,'industry'))
        self.assertEqual(len(self.adapter.served),1)
        self.assertEqual(fixture.fixture.MemoTimeoutRecoveryTests.fee_rows(self),fees)

    def test_all_metered_attempts_account_and_next_run_replay_is_zero_new_cost(self):
        self.metered();self.install(3000000)
        old=self.model.call(**fixture.DeepGateAccountingTests.kw(self,'industry'))
        self.assertEqual(len(self.adapter.served),3)
        self.assertEqual(self.account.snapshot()[2]['accounted_micros'],1200000)
        self.assertEqual(self.account.last['current_run_cost']['settled_micros'],1200000)
        self.account=fixture.DeepGateAccountingTests.accounting(self)
        replay=self.account.replay(fixture.DeepGateAccountingTests.kw(self,'industry'))
        self.assertEqual(replay['work_order_ref'],old['work_order_ref'])
        self.assertEqual(replay['cost_micros'],0)
        self.assertEqual(self.account.snapshot()[2]['current_run_cost']['settled_micros'],0)
        self.assertEqual(self.account.last['accounted_micros'],1200000)

    def test_guard_is_deny_only_and_does_not_replace_normal_day_gate(self):
        self.model._run_cost_admission_guard=lambda **_:True
        self.mission['budget']['max_daily_cost_usd']=0.000001
        with self.assertRaises(CockpitModelError):
            self.model.call(**fixture.DeepGateAccountingTests.kw(self,'industry'))
        self.assertEqual(self.adapter.served,[])
