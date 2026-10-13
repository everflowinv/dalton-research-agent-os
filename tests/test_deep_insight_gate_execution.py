import unittest
from unittest.mock import Mock
from tests import test_deep_insight_gate_accounting as fixture
from dalton_core.deep_insight_gate_execution import GateExecution,GateExecutionStopped
from dalton_core.deep_insight_gate_quiesce import GateQuiesced
from dalton_core.deep_insight_gate import GROUPS


class DeepGateBoundaryTests(unittest.TestCase):
    def setUp(self):
        fixture.DeepGateAccountingTests.setUp(self)
        self.account=self.accounting()

    kw=fixture.DeepGateAccountingTests.kw
    accounting=fixture.DeepGateAccountingTests.accounting

    def execution(self,quiesce=None):
        return GateExecution(self.model,self.account,run_micros=3000000,verifier_micros=500000,quiesce=quiesce)

    def test_original_success_free_on_new_run_and_new_group_normal_gates(self):
        run=self.execution().stage('normal','industry',future_groups=3)
        first=run.call(**self.kw('industry'));fees=fixture.fixture.MemoTimeoutRecoveryTests.fee_rows(self)
        replay=self.execution().stage('normal','industry',future_groups=3).call(**self.kw('industry'))
        self.assertEqual(replay['work_order_ref'],first['work_order_ref']);self.assertEqual(replay['cost_micros'],0)
        self.assertEqual(fixture.fixture.MemoTimeoutRecoveryTests.fee_rows(self),fees)
        self.execution().stage('normal','company',future_groups=2).call(**self.kw('company'))
        self.assertEqual(len(self.adapter.served),2)

    def test_unknown_first_group_prevents_next_group_and_new_budget_epoch(self):
        self.adapter.script={'profile:gpt-6-astra':{'code':'TIMEOUT','message':'host timeout'}}
        with self.assertRaisesRegex(GateExecutionStopped,'systemic'):
            self.execution().stage('normal','industry').call(**self.kw('industry'))
        old=fixture.fixture.MemoTimeoutRecoveryTests.fee_rows(self);count=len(self.adapter.served)
        self.adapter.script={}
        self.model.config['purpose_call_budgets']['deep_insight_gate']['max_output_tokens']=600
        with self.assertRaisesRegex(GateExecutionStopped,'unknown'):
            self.execution().stage('normal','company').call(**self.kw('company'))
        self.assertEqual(len(self.adapter.served),count);self.assertEqual(fixture.fixture.MemoTimeoutRecoveryTests.fee_rows(self),old)

    def test_all_producer_repair_and_verifier_entry_points_check_pause_before_buying(self):
        for role in ('normal','shape','numbers','rubric','verifier'):
            q=Mock();q.checkpoint.side_effect=GateQuiesced({'proof':'test'},[])
            g='all' if role=='verifier' else 'industry'
            kwargs=self.kw('industry')
            if role!='normal':kwargs={**kwargs,'request_id':'contract-repair:'+role,'prompt':'repair '+role}
            if role=='verifier':kwargs={**kwargs,'purpose':'deep_insight_gate_verifier','request_id':'verify-one','prompt':'verdict','producer_route_decision_refs':('route:producer',)}
            with self.assertRaises(GateQuiesced):self.execution(q).stage(role,g).call(**kwargs)
            self.assertEqual(self.adapter.served,[])

    def paid_retry_adapter(self):
        self.model.config['provider_retry']['retry_backoff_seconds']=0
        from dataclasses import replace
        from tests.test_cockpit_model_fallback import ReturnedProviderSequenceAdapter
        class Metered(ReturnedProviderSequenceAdapter):
            def execute(self,*args):
                invocation,result=super().execute(*args)
                return replace(invocation,usage={'raw_provider_telemetry':{'cost':{'available':True,'usd':0.4}}}),result
        adapter=Metered(['RATE_LIMITED','PROVIDER_INTERNAL_ERROR'])
        self.model.adapter_factory=lambda _:adapter
        return adapter

    def test_paid_retry_second_admission_refused_before_send_and_original_fee_retained(self):
        from dalton_core.cockpit_model import CockpitModelError
        from dalton_core.scheduler import Scheduler
        self.model.config['purpose_run_budgets']={'deep_insight_gate':{'max_cost_usd':0.7,'max_units':4}}
        self.account=self.accounting()  # existing original $3 scope: current cap tightens
        adapter=self.paid_retry_adapter()
        run=GateExecution(self.model,self.account,run_micros=700000,verifier_micros=0).stage('normal','industry')
        with self.assertRaisesRegex(CockpitModelError,'original run budget'):
            run.call(**self.kw('industry'))
        self.assertEqual(len(adapter.served),1)
        proof=self.account.snapshot()[2]
        self.assertEqual(proof['accounted_micros'],400000)
        self.assertEqual(proof['current_run_cost']['settled_micros'],400000)
        self.assertEqual(proof['unknown_work_refs'],[])
        with Scheduler(self.model.scheduler_db) as scheduler:
            work=proof['bills'][0]['work_ref'];formal=scheduler.formal_result(work)
            self.assertEqual(formal['result_envelope']['error']['code'],'BUDGET_REFUSED')
        fees=fixture.fixture.MemoTimeoutRecoveryTests.fee_rows(self)
        with self.assertRaises(CockpitModelError):run.call(**self.kw('industry'))
        self.assertEqual(len(adapter.served),1)
        self.assertEqual(fixture.fixture.MemoTimeoutRecoveryTests.fee_rows(self),fees)

    def test_paid_retry_full_costs_are_current_fee_and_replay_is_zero_new_fee(self):
        adapter=self.paid_retry_adapter()
        run=GateExecution(self.model,self.account,run_micros=3000000,verifier_micros=0).stage('normal','industry')
        first=run.call(**self.kw('industry'))
        self.assertEqual(len(adapter.served),3)
        self.assertEqual(self.account.snapshot()[2]['current_run_cost']['settled_micros'],1200000)
        old=fixture.fixture.MemoTimeoutRecoveryTests.fee_rows(self)
        account=self.accounting()
        replay=GateExecution(self.model,account,run_micros=3000000,verifier_micros=0).stage('normal','industry').call(**self.kw('industry'))
        self.assertEqual(replay['work_order_ref'],first['work_order_ref']);self.assertEqual(replay['cost_micros'],0)
        self.assertEqual(account.snapshot()[2]['accounted_micros'],1200000)
        self.assertEqual(account.last['current_run_cost']['settled_micros'],0)
        self.assertEqual(fixture.fixture.MemoTimeoutRecoveryTests.fee_rows(self),old)

    def test_request_arriving_inside_current_call_allows_formal_settlement_then_stops(self):
        q=Mock();q.returned.side_effect=GateQuiesced({'proof':'test'},[])
        with self.assertRaises(GateQuiesced):self.execution(q).stage('normal','industry').call(**self.kw('industry'))
        self.assertEqual(len(self.adapter.served),1)
        _,_,proof=self.account.snapshot();self.assertTrue(proof['cost_complete'])
        self.assertEqual(len(proof['bills']),1);self.assertFalse(proof['unknown_work_refs'])
        self.assertTrue(proof['bills'][0]['fees'][0]['settlement_ref'])

    def test_paid_attempts_are_added_to_last_unknown_ceiling_not_absorbed(self):
        from dataclasses import replace
        from tests.test_cockpit_model_fallback import ReturnedProviderSequenceAdapter
        self.model.config['provider_retry']['retry_backoff_seconds']=0
        class Metered(ReturnedProviderSequenceAdapter):
            def execute(inner,*args):
                invocation,result=super().execute(*args)
                return replace(invocation,usage={'raw_provider_telemetry':{'cost':{'available':True,'usd':0.4}}}),result
        adapter=Metered(['RATE_LIMITED','PROVIDER_INTERNAL_ERROR','TIMEOUT'])
        self.model.adapter_factory=lambda _:adapter
        with self.assertRaises(GateExecutionStopped):
            GateExecution(self.model,self.account,run_micros=3000000,verifier_micros=0).stage('normal','industry').call(**self.kw('industry'))
        bill=self.account.snapshot()[2]['bills'][0]
        earlier=sum(f['settled_micros']for f in bill['fees'][:-1])
        last=bill['fees'][-1]
        self.assertGreaterEqual(bill['accounted_micros'],earlier+max(500000,last['reserved_micros'],last['settled_micros']))
        self.assertEqual(len(bill['fees']),3)
