import unittest
from unittest.mock import patch
from tests import test_investment_memo_timeout_recovery as fixture
from dalton_core.deep_insight_gate_accounting import DeepGateAccounting,normal_request_id,SCOPES,REQUESTS
from dalton_core.deep_insight_gate import GROUPS
from dalton_core import deep_insight_gate_draft  # registers the actual producer purposes
from dalton_core.cockpit_model import CockpitModelError
from dalton_core.scheduler import Scheduler


class DeepGateAccountingTests(unittest.TestCase):
    def setUp(self):
        fixture.MemoTimeoutRecoveryTests.setUp(self)
        self.model.config['purpose_call_budgets']['deep_insight_gate']={
            'max_input_tokens':120000,'max_output_tokens':500,'max_cost_usd':0.5,'timeout_seconds':180}
        self.prompts={g:'Frozen company:one '+g+' actual source:one' for g in GROUPS}

    def kw(self,g):
        return dict(purpose='deep_insight_gate',request_id=normal_request_id(g,'company:one',self.prompts[g]),
                    prompt=self.prompts[g],mission=self.mission)

    def accounting(self):
        return DeepGateAccounting(self.model,self.prompts,company_ref='company:one')

    def test_four_prior_groups_known_before_next_call_and_original_fee_rows_unchanged(self):
        for company in ('company:one','company:two'):
            self.prompts={g:company+' '+g+' source:one' for g in GROUPS}
            a=DeepGateAccounting(self.model,self.prompts,company_ref=company)
            for g in GROUPS:
                self.model.call(purpose='deep_insight_gate',request_id=normal_request_id(g,company,self.prompts[g]),prompt=self.prompts[g],mission=self.mission)
            old=fixture.MemoTimeoutRecoveryTests.fee_rows(self)
            b=DeepGateAccounting(self.model,self.prompts,company_ref=company)
            _,_,proof=b.snapshot()
            self.assertTrue(proof['cost_complete']);self.assertEqual(len(proof['bills']),4)
            self.assertGreater(proof['accounted_micros'],0)
            self.assertEqual(fixture.MemoTimeoutRecoveryTests.fee_rows(self),old)

    def test_register_before_dispatch_crash_remains_new_scope(self):
        a=self.accounting();b=self.accounting()
        self.assertEqual(a.ref,b.ref);self.assertTrue(b.snapshot()[2]['cost_complete'])
        self.model.call(**self.kw('industry'))
        self.assertTrue(self.accounting().snapshot()[2]['cost_complete'])

    def test_old_unindexed_work_never_claims_complete_legacy_history(self):
        self.model.call(**self.kw('industry'))
        a=self.accounting();p=a.snapshot()[2]
        self.assertFalse(p['cost_complete']);self.assertEqual(p['status'],'legacy_full_fee_import_required')
        self.assertGreater(p['accounted_micros'],0)

    def test_budget_mission_and_run_directory_do_not_reset_frozen_scope(self):
        a=self.accounting()
        self.model.config['purpose_run_budgets']={'deep_insight_gate':{'max_cost_usd':10}}
        self.mission={**self.mission,'id':'mission:new','content_hash':'a'*64}
        self.assertEqual(self.accounting().ref,a.ref)
        self.prompts={g:p+' NEW ACTUAL SOURCE' for g,p in self.prompts.items()}
        self.assertNotEqual(self.accounting().ref,a.ref)

    def test_timeout_unknown_conservatively_retained_not_summary_zero(self):
        a=self.accounting()
        self.adapter.script={'profile:gpt-6-astra':{'code':'TIMEOUT','message':'host timeout'}}
        with self.assertRaises(CockpitModelError):self.model.call(**self.kw('industry'))
        old=fixture.MemoTimeoutRecoveryTests.fee_rows(self)
        p=a.snapshot()[2]
        self.assertTrue(p['cost_complete']);self.assertEqual(len(p['unknown_work_refs']),1)
        self.assertGreaterEqual(p['accounted_micros'],500000)
        self.assertEqual(fixture.MemoTimeoutRecoveryTests.fee_rows(self),old)

    def test_missing_terminal_formal_is_inflight_never_typeerror_or_free(self):
        a=self.accounting()
        self.model.call(**self.kw('industry'))
        with patch('dalton_core.scheduler.Scheduler.formal_result',return_value=None):
            with self.assertRaisesRegex(ValueError,'in-flight Work'):a.snapshot()
        self.assertEqual(a.last['status'],'in_flight_or_unproved')
        self.assertGreater(a.last['retained_cap_micros'],0)

    def test_requests_append_only_and_only_one_correction_per_role_group(self):
        a=self.accounting()
        k={**self.kw('industry'),'request_id':'contract-repair:one','prompt':'one'}
        a.register(role='shape',group='industry',kwargs=k)
        a.register(role='shape',group='industry',kwargs=k)
        with self.assertRaisesRegex(ValueError,'generation'):
            a.register(role='shape',group='industry',kwargs={**k,'request_id':'contract-repair:two','prompt':'two'})
        with Scheduler(self.model.scheduler_db)as s:
            with self.assertRaisesRegex(Exception,'append-only'):
                s.connection.execute('DELETE FROM '+REQUESTS)
            self.assertEqual(s.connection.execute('SELECT count(*) FROM '+SCOPES).fetchone()[0],1)

    def test_old_success_can_replay_free_while_legacy_extra_purchase_is_refused(self):
        old=self.model.call(**self.kw('industry'));fees=fixture.MemoTimeoutRecoveryTests.fee_rows(self)
        a=self.accounting();answer=a.replay(self.kw('industry'))
        self.assertTrue(answer['replayed']);self.assertEqual(answer['cost_micros'],0)
        self.assertEqual(answer['work_order_ref'],old['work_order_ref'])
        with self.assertRaisesRegex(ValueError,'legacy full fee import'):
            a.before_paid(run_micros=3000000,next_call_micros=500000)
        self.assertEqual(fixture.MemoTimeoutRecoveryTests.fee_rows(self),fees)

    def test_systemic_unknown_prevents_next_stage_and_changed_budget_repurchase(self):
        a=self.accounting()
        self.adapter.script={'profile:gpt-6-astra':{'code':'TIMEOUT','message':'host timeout'}}
        with self.assertRaises(CockpitModelError):self.model.call(**self.kw('industry'))
        fees=fixture.MemoTimeoutRecoveryTests.fee_rows(self);count=len(self.adapter.served)
        self.model.config['purpose_call_budgets']['deep_insight_gate']['max_output_tokens']=600
        with self.assertRaisesRegex(ValueError,'systemic unknown'):
            self.accounting().before_paid(run_micros=3000000,next_call_micros=500000)
        with self.assertRaises(CockpitModelError):self.accounting().replay(self.kw('industry'))
        self.assertEqual(len(self.adapter.served),count);self.assertEqual(fixture.MemoTimeoutRecoveryTests.fee_rows(self),fees)

    def test_remaining_stage_reservations_stop_a_call_that_would_exceed_macro(self):
        a=self.accounting();self.model.call(**self.kw('industry'))
        with self.assertRaisesRegex(ValueError,'original run budget'):
            a.before_paid(run_micros=1000000,next_call_micros=500000,retained_future_micros=500000)
        self.assertEqual(len(self.adapter.served),1)
