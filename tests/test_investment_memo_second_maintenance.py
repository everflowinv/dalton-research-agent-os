"""Shared policy permits one additional, source-bound correction, never reset."""
import copy
import json
import unittest
from dalton_core import investment_memo_second_maintenance as second
from dalton_core.investment_memo_group_maintenance import TABLE,record_for
from dalton_core.scheduler import Scheduler
from dalton_core.shared_call_budget_policy import updated_shared_limits
from dalton_core.call_budget import MEMO_MAINTENANCE_ROUNDS_FIELD as FIELD
from tests import test_investment_memo_group_maintenance as fixtures
from tests.test_global_model_budget_limits import legacy_policy


class MemoSecondMaintenanceTests(unittest.TestCase):
    def fixture(self,company='company:synthetic-a'):
        f=fixtures.MemoGroupMaintenanceTests('runTest');f.setUp();self.addCleanup(f.doCleanups)
        f.contexts=f.make_contexts(company)
        from dalton_core.investment_memo_draft import build_group_prompt
        f.prompts=[build_group_prompt(**c) for c in f.contexts]
        f.shared_path=f.root/'shared-policy.json'
        f.model.config['shared_call_budget_policy_path']=str(f.shared_path)
        self.set_policy(f,1)
        return f

    def set_policy(self,f,rounds):
        old=json.loads(f.shared_path.read_text()) if f.shared_path.exists() else legacy_policy()
        value=updated_shared_limits(old,purpose='investment_memo',kind='run',
            budget={FIELD:rounds},actor_ref='human:owner',updated_at='2026-10-13T08:00:00+00:00')
        f.shared_path.write_text(json.dumps(value));return value

    def first_failed(self,f):
        wrapper,bad=f.seeded();f.bad_maintenance={1};first,bad=f.purchase(wrapper,bad)
        self.assertEqual(bad['status'],'refused');return wrapper,first,bad

    def test_two_companies_exact_second_parent_old_fees_restart_and_policy_reduction(self):
        for company in ('company:synthetic-a','company:synthetic-b'):
            with self.subTest(company=company):
                f=self.fixture(company);wrapper,first,bad=self.first_failed(f)
                old=f.fees();paid=len(f.adapter.served)
                self.assertIsNone(wrapper.prepare_maintenance(context=f.contexts[1],outcome=bad,remaining_groups=2))
                self.assertEqual(len(f.adapter.served),paid)
                self.set_policy(f,2);wrapper=f.wrapper()
                bad=f.outcome(wrapper,1);body,value=f.purchase(wrapper,bad)
                self.assertEqual(value['status'],'drafted');self.assertEqual(body['contract_ref'],second.CONTRACT_REF)
                self.assertEqual(body['permission_ref'],first['permission_ref']+':round2')
                self.assertEqual(body['parent']['work_order_ref'],bad['model']['work_order_ref'])
                self.assertEqual(len(f.adapter.served),paid+1)
                new=f.fees();self.assertEqual(new[0][:len(old[0])],old[0]);self.assertEqual(new[1][:len(old[1])],old[1])
                self.set_policy(f,1)
                for _ in range(2):
                    fresh=f.wrapper();self.assertEqual(f.outcome(fresh,1)['status'],'drafted')
                self.assertEqual(len(f.adapter.served),paid+1);self.assertEqual(f.fees(),new)
                with Scheduler(f.model.scheduler_db) as s:
                    self.assertEqual(s.connection.execute(f'SELECT count(*) FROM {TABLE}').fetchone()[0],2)

    def test_issued_second_after_budget_defer_needs_current_enabled_policy(self):
        f=self.fixture();wrapper,first,bad=self.first_failed(f);self.set_policy(f,2)
        body=wrapper.prepare_maintenance(context=f.contexts[1],outcome=bad,remaining_groups=2)
        kwargs={'purpose':'investment_memo','request_id':body['entry']['request_id'],
                'prompt':body['prompt'],'mission':f.mission}
        wrapper.macro.register_maintenance(body,kwargs)
        old=f.fees();paid=len(f.adapter.served);self.set_policy(f,1)
        with self.assertRaisesRegex(ValueError,'only one correction'):
            wrapper.macro.before_paid(kwargs,run_micros=112000000,
                producer_micros=14000000,verifier_micros=14000000,role='maintenance')
        self.assertEqual(len(f.adapter.served),paid);self.assertEqual(f.fees(),old)

    def test_forged_diagnostic_foreign_context_and_normal_parent_cannot_issue_second(self):
        f=self.fixture();wrapper,first,bad=self.first_failed(f);self.set_policy(f,2)
        old=f.fees();paid=len(f.adapter.served)
        for mutation in ({'company':{'company_ref':'company:foreign'}},{'material':[]}):
            context={**f.contexts[1],**mutation}
            with self.assertRaises(ValueError):second.derive(wrapper.macro,context,bad['model']['work_order_ref'])
        original=f.outcome(f.wrapper(),0)
        with self.assertRaises(ValueError):second.derive(wrapper.macro,f.contexts[1],original['model']['work_order_ref'])
        body=second.derive(wrapper.macro,f.contexts[1],bad['model']['work_order_ref'])
        body['diagnostics']['findings']=[]
        with self.assertRaisesRegex(ValueError,'diagnostics/source'):
            wrapper.macro.register_maintenance(body,{'purpose':'investment_memo','request_id':body['entry']['request_id'],
                'prompt':body['prompt'],'mission':f.mission})
        self.assertEqual(len(f.adapter.served),paid);self.assertEqual(f.fees(),old)

    def test_local_override_and_unknown_shared_policy_never_enable_second(self):
        f=self.fixture();wrapper,first,bad=self.first_failed(f)
        f.model.config['purpose_run_budgets']={'investment_memo':{FIELD:2}}
        with self.assertRaises(ValueError):second.derive(wrapper.macro,f.contexts[1],bad['model']['work_order_ref'])
        value=json.loads(f.shared_path.read_text());value['schema_version']='unknown'
        f.shared_path.write_text(json.dumps(value))
        with self.assertRaises(ValueError):second.derive(wrapper.macro,f.contexts[1],bad['model']['work_order_ref'])

    def test_failed_second_uses_final_fixed_slot_restart_and_new_budget_never_buy_third(self):
        from dataclasses import replace
        f=self.fixture();wrapper,first,bad=self.first_failed(f);self.set_policy(f,2)
        original=f.adapter.execute
        def answer(work,route,profile):
            invocation,envelope=original(work,route,profile)
            if second.CONTRACT_REF in work.question and envelope.status=='succeeded':
                value=json.loads(envelope.outputs['text']);value['sections'][0]['body']+=' 未引证金额 999.7。'
                envelope=replace(envelope,outputs={'text':json.dumps(value,ensure_ascii=False)})
            return invocation,envelope
        f.adapter.execute=answer
        body,refused=f.purchase(wrapper,bad);self.assertEqual(refused['failure_class'],'content')
        old=f.fees();paid=len(f.adapter.served)
        for ceiling in (112000000,168000000):
            fresh=f.wrapper(run=ceiling);again=f.outcome(fresh,1)
            self.assertIsNone(fresh.prepare_maintenance(context=f.contexts[1],outcome=again,remaining_groups=2))
        self.assertEqual(f.fees(),old);self.assertEqual(len(f.adapter.served),paid)

    def test_body_diagnostics_wake_and_budget_restart_never_reset_paid_second(self):
        from dataclasses import replace
        from unittest.mock import patch
        from dalton_core import investment_memo_number_diagnostics as diagnostic
        f=self.fixture();wrapper,first,bad=self.first_failed(f);self.set_policy(f,2)
        original=f.adapter.execute
        def answer(work,route,profile):
            invocation,envelope=original(work,route,profile)
            if second.CONTRACT_REF in work.question and envelope.status=='succeeded':
                value=json.loads(envelope.outputs['text']);s=value['sections'][0]
                s['body']+=' 利润预期为 15.55。'
                s['numbers'].append({'text':'15.55','estimate':{'assumption_ref':'assumption:a'}})
                envelope=replace(envelope,outputs={'text':json.dumps(value,ensure_ascii=False)})
            return invocation,envelope
        f.adapter.execute=answer
        body,refused=f.purchase(wrapper,bad)
        self.assertIn('without saying',refused['reason'])
        old=f.fees();paid=len(f.adapter.served)
        with patch.object(diagnostic,'CONTRACT_REF','investment-memo-complete-section-diagnostics:test'):
            for ceiling in (112000000,168000000):
                fresh=f.wrapper(run=ceiling);again=f.outcome(fresh,1)
                self.assertIsNone(fresh.prepare_maintenance(context=f.contexts[1],outcome=again,remaining_groups=2))
                self.assertIn('without saying',again['reason'])
        self.assertEqual(f.fees(),old);self.assertEqual(len(f.adapter.served),paid)

    def test_second_reserves_old_charges_remaining_groups_and_independent_verifier(self):
        f=self.fixture();wrapper,first,bad=self.first_failed(f);self.set_policy(f,2)
        old=f.fees();paid=len(f.adapter.served)
        fresh=f.wrapper(run=40000000)
        self.assertIsNone(fresh.prepare_maintenance(context=f.contexts[1],outcome=bad,remaining_groups=2))
        self.assertEqual(fresh.maintenance_hold['status'],'budget_refused')
        self.assertEqual(fresh.maintenance_hold['future_reserved_micros'],42000000)
        self.assertEqual(f.fees(),old);self.assertEqual(len(f.adapter.served),paid)

    def test_unknown_paid_second_preserves_charge_and_never_authorizes_next_round(self):
        from dalton_core.cockpit_model import CockpitModelError
        f=self.fixture();wrapper,first,bad=self.first_failed(f);self.set_policy(f,2)
        body=wrapper.prepare_maintenance(context=f.contexts[1],outcome=bad,remaining_groups=2)
        f.adapter.script={'profile:gpt-6-astra':{'code':'TIMEOUT','message':'unknown'}}
        wrapper.begin_stage(max_units=4,allow_technical=False,maintenance=body)
        from dalton_core.investment_memo_draft import draft_group
        with self.assertRaises(CockpitModelError):
            wrapper.call(purpose='investment_memo',request_id=body['entry']['request_id'],
                prompt=body['prompt'],mission=f.mission)
        paid=len(f.adapter.served);f.adapter.script={}
        fresh=f.wrapper(run=168000000)
        self.assertIsNone(fresh.prepare_maintenance(context=f.contexts[1],outcome=bad,remaining_groups=2))
        self.assertGreaterEqual(fresh.macro.last['accounted_micros'],14000000)
        self.assertEqual(len(f.adapter.served),paid)

    def test_internal_second_provider_attempt_cannot_spend_reserved_future_room(self):
        from unittest.mock import patch
        from dalton_core.cockpit_model import CockpitModelError
        from tests.test_cockpit_model_fallback import ReturnedProviderSequenceAdapter
        f=self.fixture();wrapper,first,bad=self.first_failed(f);self.set_policy(f,2)
        body=wrapper.prepare_maintenance(context=f.contexts[1],outcome=bad,remaining_groups=2)
        adapter=ReturnedProviderSequenceAdapter(['RATE_LIMITED','PROVIDER_INTERNAL_ERROR'])
        f.model.adapter_factory=lambda router:adapter
        fresh=f.wrapper(run=65000000);fresh.begin_stage(max_units=4,allow_technical=False,maintenance=body)
        old=f.fees()
        with patch('time.sleep',side_effect=lambda seconds:f.clock.advance(seconds)):
            with self.assertRaises(CockpitModelError):
                fresh.call(purpose='investment_memo',request_id=body['entry']['request_id'],
                    prompt=body['prompt'],mission=f.mission)
        self.assertEqual(len(adapter.served),1)
        self.assertEqual(f.fees()[0][:len(old[0])],old[0])
        self.assertEqual(f.fees()[1][:len(old[1])],old[1])

    def test_full_four_group_run_second_correction_and_independent_verifier(self):
        f=self.fixture();wrapper,first,bad=self.first_failed(f);self.set_policy(f,2)
        old=f.fees();result,done=f.full_run()
        self.assertEqual(result['status'],'succeeded',result)
        self.assertEqual(len(f.adapter.served),8)
        new=f.fees();self.assertEqual(new[0][:len(old[0])],old[0]);self.assertEqual(new[1][:len(old[1])],old[1])
        paid=len(f.adapter.served);replayed,fresh=f.full_run()
        self.assertEqual(replayed['status'],'succeeded',replayed)
        self.assertEqual(replayed['cost_micros'],0);self.assertEqual(f.fees(),new)
        self.assertEqual(len(f.adapter.served),paid)
