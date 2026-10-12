import unittest
import sqlite3
from unittest.mock import patch
from dalton_core.cockpit_model import CockpitModelError
from dalton_core.investment_memo_repair_budget import MemoRepairBudget
from dalton_core.investment_memo_macro_accounting import request_id, SCOPES, REQUESTS, EXTRA
from dalton_core.scheduler import Scheduler
from tests import test_investment_memo_timeout_recovery as fixture


class MemoMacroAccountingTests(unittest.TestCase):
    def setUp(self):
        fixture.MemoTimeoutRecoveryTests.setUp(self)
        self.prompts = [fixture.MemoTimeoutRecoveryTests.kw(self,i)['prompt'] for i in range(4)]

    def kw(self, i):
        from dalton_core.investment_memo_draft import GROUPS
        return {'purpose':'investment_memo', 'request_id':request_id(GROUPS[i][0], self.prompts[i]),
                'prompt':self.prompts[i], 'mission':self.mission}

    def wrapper(self, *, run=84_000_000, technical=False):
        result = MemoRepairBudget(self.model, run_micros=run, producer_micros=14_000_000,
                                  verifier_micros=14_000_000)
        result.bind_macro(self.prompts,self.mission)
        result.begin_stage(max_units=4,allow_technical=technical)
        return result

    def repair(self, reason='wrong reference'):
        prompt = self.prompts[0]+'\nYour previous answer for this group was refused and was not used:\n'+reason
        return {'purpose':'investment_memo', 'request_id':request_id('identity_background',prompt),
                'prompt':prompt, 'mission':self.mission}

    def fees(self):
        return fixture.MemoTimeoutRecoveryTests.fee_rows(self)

    def test_all_four_prior_bills_are_known_before_first_group_repair_without_double_charge(self):
        for company in ('company:one','company:two'):
            self.prompts = [fixture.MemoTimeoutRecoveryTests.kw(self,i,company)['prompt'] for i in range(4)]
            first = self.wrapper()
            for i in range(4): first.call(**self.kw(i))
            original = self.fees(); calls = len(self.adapter.served)
            second = self.wrapper(); answer = second.call(**self.kw(0))
            self.assertEqual(answer['cost_micros'],0)
            self.assertEqual(second.accounted_micros, first.accounted_micros)
            self.assertEqual(second.macro.last['historical_work_count'],4)
            second.prepare_repair(self.repair())
            self.assertTrue(second.can_repair(remaining_groups=3))
            second.begin_stage(max_units=4,allow_technical=False,repair=True)
            repaired = second.call(**self.repair())
            self.assertEqual(len(self.adapter.served),calls+1)
            self.assertGreater(second.accounted_micros,first.accounted_micros)
            self.assertEqual(self.fees()[0][:len(original[0])],original[0])
            self.assertEqual(self.fees()[1][:len(original[1])],original[1])
            third=self.wrapper();third.call(**self.kw(0));third.prepare_repair(self.repair())
            self.assertTrue(third.can_repair(remaining_groups=3))
            third.begin_stage(max_units=4,allow_technical=False,repair=True)
            self.assertEqual(third.call(**self.repair())['cost_micros'],0)
            self.assertEqual(third.accounted_micros,second.accounted_micros)
            self.assertEqual(len(self.adapter.served),calls+1)

    def test_different_old_repair_reason_cannot_get_a_second_semantic_generation(self):
        first=self.wrapper();first.call(**self.kw(0));first.prepare_repair(self.repair())
        self.assertTrue(first.can_repair(remaining_groups=3))
        first.begin_stage(max_units=4,allow_technical=False,repair=True);first.call(**self.repair())
        old=self.fees();count=len(self.adapter.served)
        next_run=self.wrapper();next_run.call(**self.kw(0));next_run.prepare_repair(self.repair('different reason'))
        self.assertFalse(next_run.can_repair(remaining_groups=3))
        next_run.begin_stage(max_units=4,allow_technical=False,repair=True)
        with self.assertRaises(CockpitModelError):next_run.call(**self.repair('different reason'))
        self.assertEqual(self.fees(),old);self.assertEqual(len(self.adapter.served),count)

    def test_complete_bounded_legacy_import_reuses_paid_group_and_keeps_the_old_scope_unchanged(self):
        self.model.call(**self.kw(0));old=self.fees();count=len(self.adapter.served)
        wrapper=self.wrapper();self.assertTrue(wrapper.macro.complete)
        self.assertFalse(wrapper.macro.scope['complete'])
        self.assertEqual(wrapper.call(**self.kw(0))['cost_micros'],0)
        wrapper.prepare_repair(self.repair());self.assertTrue(wrapper.can_repair(remaining_groups=3))
        wrapper=self.wrapper();self.assertTrue(wrapper.macro.complete)
        self.assertEqual(self.fees(),old);self.assertEqual(len(self.adapter.served),count)

    def test_registered_dispatch_gap_is_not_misclassified_as_legacy_on_restart(self):
        first=self.wrapper();ref=first.macro.ref
        # Intent already exists even if caller crashed before its first call.
        self.model.call(**self.kw(0))
        resumed=self.wrapper();self.assertTrue(resumed.macro.complete);self.assertEqual(resumed.macro.ref,ref)
        self.assertEqual(resumed.call(**self.kw(0))['cost_micros'],0)
        with Scheduler(self.model.scheduler_db) as s:
            self.assertEqual(s.connection.execute('SELECT count(*) FROM '+SCOPES).fetchone()[0],1)
            self.assertEqual(s.connection.execute('SELECT count(*) FROM '+REQUESTS).fetchone()[0],4)

    def test_real_no_send_route_refusal_keeps_original_configuration_reentry(self):
        self.model.config['purpose_call_budgets']['investment_memo']['max_output_tokens']=300000
        first=self.wrapper()
        with self.assertRaises(CockpitModelError):first.call(**self.kw(0))
        self.assertEqual(self.adapter.served,[])
        self.assertEqual(self.fees(),([],[]))
        self.model.config['purpose_call_budgets']['investment_memo']['max_output_tokens']=500
        resumed=self.wrapper()
        answer=resumed.call(**self.kw(0))
        with Scheduler(self.model.scheduler_db) as scheduler:
            self.assertEqual(scheduler.formal_result(answer['work_order_ref'])['terminal_state'],'succeeded')
        self.assertEqual(len(self.adapter.served),1)
        self.assertEqual(resumed.macro.last['historical_work_count'],2)
        self.assertEqual(len(self.fees()[0]),1)
        count=len(self.adapter.served)
        self.assertEqual(self.wrapper().call(**self.kw(0))['cost_micros'],0)
        self.assertEqual(len(self.adapter.served),count)

    def test_frozen_input_changes_create_new_macro_but_budget_and_mission_versions_do_not(self):
        first=self.wrapper();original=first.macro.ref
        self.model.config['purpose_run_budgets']={'investment_memo':{'max_cost_usd':98}}
        self.mission={**self.mission,'id':'coverage-mission-version:new','content_hash':'a'*64}
        changed=self.wrapper();self.assertEqual(changed.macro.ref,original)
        self.prompts=[p.replace('Evidence rows:\n[]','Evidence rows:\n[{"ref":"claim:new","text":"new evidence"}]') for p in self.prompts]
        fresh=self.wrapper();self.assertNotEqual(fresh.macro.ref,original)
        self.assertTrue(fresh.macro.complete)

    def test_future_timeout_reservation_is_accounted_before_first_free_replay(self):
        first=self.wrapper();first.call(**self.kw(0));first.call(**self.kw(1))
        self.adapter.script={'profile:gpt-6-astra':{'code':'TIMEOUT','message':'host timeout'}}
        with self.assertRaises(CockpitModelError):first.call(**self.kw(2))
        old=self.fees();self.clock.advance(61);self.adapter.script={};count=len(self.adapter.served)
        next_run=self.wrapper();next_run.call(**self.kw(0))
        self.assertGreaterEqual(next_run.accounted_micros,14_000_000)
        self.assertEqual(next_run.macro.last['historical_work_count'],3)
        next_run.prepare_repair(self.repair());self.assertTrue(next_run.can_repair(remaining_groups=3))
        next_run.begin_stage(max_units=4,allow_technical=False,repair=True);next_run.call(**self.repair())
        next_run.begin_stage(max_units=4,allow_technical=True)
        with self.assertRaises(CockpitModelError):next_run.call(**self.kw(2))
        self.assertEqual(len(self.adapter.served),count+1)
        self.assertEqual(self.fees()[0][:len(old[0])],old[0]);self.assertEqual(self.fees()[1][:len(old[1])],old[1])

    def test_unsettled_future_work_does_not_buy_new_repair_or_erase_reservation(self):
        wrapper=self.wrapper();wrapper.call(**self.kw(0))
        self.adapter.script={'profile:gpt-6-astra':{'code':'TIMEOUT','message':'unknown timeout'}}
        with patch('dalton_core.cockpit_model._settle_without_losing_the_lease',return_value=None):
            with self.assertRaises(CockpitModelError):wrapper.call(**self.kw(1))
        old=self.fees();count=len(self.adapter.served)
        resumed=self.wrapper();resumed.call(**self.kw(0));resumed.prepare_repair(self.repair())
        self.assertFalse(resumed.can_repair(remaining_groups=3))
        self.assertIsNone(resumed.macro_error)
        self.assertEqual(resumed.macro.last['fee_certainty'],'conservative_upper_bound')
        self.assertGreaterEqual(resumed.accounted_micros,14_000_000)
        self.assertEqual(self.fees(),old);self.assertEqual(len(self.adapter.served),count)

    def test_extra_slot_is_atomic_and_immutable_across_independent_readers(self):
        first=self.wrapper();other=self.wrapper()
        self.adapter.script={'profile:gpt-6-astra':{'code':'TIMEOUT','message':'timeout'}}
        from dalton_core.cockpit_model import model_failure_trace
        with self.assertRaises(CockpitModelError) as failure:first.call(**self.kw(0))
        parent=model_failure_trace(failure.exception)['work_order_ref']
        first.macro.claim_technical(parent)
        with self.assertRaises(ValueError):other.macro.register(role='semantic',kwargs=self.repair())
        other.macro.claim_technical(parent)
        with self.assertRaises(ValueError):other.macro.claim_technical('work:other')
        with Scheduler(self.model.scheduler_db) as s:
            self.assertEqual(s.connection.execute('SELECT count(*) FROM '+EXTRA).fetchone()[0],1)
            with self.assertRaises(sqlite3.IntegrityError):s.connection.execute('UPDATE '+SCOPES+' SET content_hash=?',('x',))

    def test_in_flight_registered_work_without_formal_is_not_dispatched_again(self):
        from dalton_core.cockpit_model import build_work
        wrapper=self.wrapper();kwargs=self.kw(0)
        work=build_work(purpose=kwargs['purpose'],request_id=kwargs['request_id'],prompt=kwargs['prompt'],
            mission_version_ref=self.mission['id'],mission_version_hash=self.mission['content_hash'],
            max_input_tokens=600000,max_output_tokens=16000,max_cost_usd=14,max_seconds=900,
            created_at=self.clock().isoformat())
        with Scheduler(self.model.scheduler_db) as s:s.enqueue(work)
        old=self.fees();count=len(self.adapter.served)
        resumed=self.wrapper();self.assertTrue(resumed.macro.complete)
        with self.assertRaisesRegex(CockpitModelError,'in flight'):resumed.call(**kwargs)
        self.assertEqual(resumed.macro.last['status'],'in_flight_fee_unknown')
        self.assertEqual(resumed.macro.last['retained_macro_reservation_micros'],14_000_000)
        self.assertFalse(resumed.macro.last['cost_complete'])
        self.assertEqual(self.fees(),old);self.assertEqual(len(self.adapter.served),count)

    def test_actual_technical_child_uses_only_extra_slot_and_is_free_on_next_run(self):
        first=self.wrapper();first.call(**self.kw(0));first.call(**self.kw(1))
        self.adapter.script={'profile:gpt-6-astra':{'code':'TIMEOUT','message':'host timeout'}}
        with self.assertRaises(CockpitModelError):first.call(**self.kw(2))
        old=self.fees();self.clock.advance(61);self.adapter.script={};count=len(self.adapter.served)
        next_run=self.wrapper(technical=True);next_run.call(**self.kw(0));next_run.call(**self.kw(1))
        answer=next_run.call(**self.kw(2))
        self.assertIn('timeout_recovery_authorization_ref',answer)
        self.assertEqual(len(self.adapter.served),count+1)
        self.assertTrue(next_run.technical_slot_consumed)
        next_run.prepare_repair(self.repair());self.assertFalse(next_run.can_repair(remaining_groups=1))
        final=self.wrapper(technical=True);final.call(**self.kw(0));final.call(**self.kw(1))
        self.assertEqual(final.call(**self.kw(2))['cost_micros'],0)
        self.assertTrue(final.technical_slot_consumed)
        self.assertEqual(len(self.adapter.served),count+1)
        self.assertGreaterEqual(final.accounted_micros,14_000_000)
        self.assertEqual(self.fees()[0][:len(old[0])],old[0]);self.assertEqual(self.fees()[1][:len(old[1])],old[1])

    def test_two_competing_extra_kinds_get_exactly_one_atomic_slot(self):
        from concurrent.futures import ThreadPoolExecutor
        from threading import Barrier
        from dalton_core.cockpit_model import model_failure_trace
        first=self.wrapper();other=self.wrapper()
        self.adapter.script={'profile:gpt-6-astra':{'code':'TIMEOUT','message':'timeout'}}
        with self.assertRaises(CockpitModelError) as failure:first.call(**self.kw(0))
        parent=model_failure_trace(failure.exception)['work_order_ref']
        barrier=Barrier(2)
        def race(kind):
            barrier.wait()
            try:
                if kind=='technical':first.macro.claim_technical(parent)
                else:other.macro.register(role='semantic',kwargs=self.repair())
                return 'accepted'
            except ValueError:return 'refused'
        with ThreadPoolExecutor(max_workers=2) as pool:
            results=list(pool.map(race,('technical','semantic')))
        self.assertCountEqual(results,['accepted','refused'])
        with Scheduler(self.model.scheduler_db) as s:
            self.assertEqual(s.connection.execute('SELECT count(*) FROM '+EXTRA).fetchone()[0],1)

    def test_verifier_fees_share_macro_and_replay_preserves_provider_independence(self):
        from dalton_core.investment_memo_repair_budget import MemoMacroVerifier
        from tests import test_cockpit_model_fallback as models
        from dalton_core.store import content_hash
        wrapper=self.wrapper();results=[wrapper.call(**self.kw(i)) for i in range(4)]
        adapter=models.ChainAdapter({})
        verifier=models.CockpitChainTests._model(self,adapter,policy_version_ref=self.verifier_policy,
                                              slots=self.verifier_slots,clock=self.clock)
        verifier.config['budget_policy_ref']='memo-test-day-policy'
        wrapper_verifier=MemoMacroVerifier(verifier,wrapper)
        refs=[r['route_decision_ref']for r in results]
        kwargs={'purpose':'investment_memo_verifier','request_id':'memo-verify-'+content_hash(refs)[:24],
                'prompt':'Independently verify the frozen four source groups','mission':self.mission}
        first=wrapper_verifier.call(producer_route_decision_refs=refs,**kwargs)
        old=self.fees();count=len(adapter.served)
        free=wrapper_verifier.call(producer_route_decision_refs=refs,**kwargs)
        self.assertEqual(free['cost_micros'],0);self.assertEqual(free['work_order_ref'],first['work_order_ref'])
        self.assertEqual(self.fees(),old);self.assertEqual(len(adapter.served),count)
        self.assertEqual(wrapper.macro.last['historical_work_count'],5)
        with self.assertRaisesRegex(CockpitModelError,'producer bindings differ'):
            wrapper_verifier.call(producer_route_decision_refs=refs[:1],**kwargs)
        with self.assertRaisesRegex(CockpitModelError,'sole verifier'):
            wrapper_verifier.call(producer_route_decision_refs=refs,**{**kwargs,'request_id':kwargs['request_id']+'-again'})
        self.assertEqual(self.fees(),old);self.assertEqual(len(adapter.served),count)

    def test_request_seeks_ignore_unrelated_history_and_enforce_fixed_query_bound(self):
        from contextlib import contextmanager
        from dataclasses import replace
        from dalton_core.cockpit_model import build_work
        from dalton_core import planner_paid_output_recovery as readers
        wrapper=self.wrapper();kwargs=self.kw(0)
        template=build_work(purpose='plan',request_id='unrelated',prompt='unrelated research',
            mission_version_ref=self.mission['id'],max_input_tokens=1000,max_output_tokens=500,
            max_cost_usd=1,max_seconds=180,created_at=self.clock().isoformat())
        with Scheduler(self.model.scheduler_db) as s:
            for i in range(200):
                s.enqueue(replace(template,id=f'work:unrelated-{i}',idempotency_key=f'unrelated-{i}',
                                  metadata={**template.metadata,'request_id':f'unrelated-{i}'}))
        statements=[];original=readers._authority_connection
        @contextmanager
        def observed(path):
            with original(path) as c:
                if str(path)==str(self.model.scheduler_db):c.set_trace_callback(statements.append)
                yield c
        with patch.object(readers,'_authority_connection',observed):
            _,_,amount=wrapper.macro.snapshot()
        seeks=[sql for sql in statements if 'AS append_rowid' in sql]
        self.assertEqual(len(seeks),4)
        self.assertTrue(all('LIMIT 17' in sql for sql in seeks))
        self.assertEqual(amount,0)
        matching=build_work(purpose=kwargs['purpose'],request_id=kwargs['request_id'],prompt=kwargs['prompt'],
            mission_version_ref=self.mission['id'],mission_version_hash=self.mission['content_hash'],
            max_input_tokens=600000,max_output_tokens=16000,max_cost_usd=14,max_seconds=900,
            created_at=self.clock().isoformat())
        with Scheduler(self.model.scheduler_db) as s:
            for i in range(17):s.enqueue(replace(matching,id=f'work:matching-{i}',idempotency_key=f'matching-{i}'))
        with self.assertRaisesRegex(CockpitModelError,'bounded16'):wrapper.call(**kwargs)
        self.assertEqual(self.adapter.served,[])

    def test_corrupt_scope_and_foreign_semantic_prompt_do_not_authorize_a_call(self):
        wrapper=self.wrapper();foreign=self.repair();foreign['prompt']=foreign['prompt'].replace('company:one','company:foreign')
        with self.assertRaises(ValueError):wrapper.macro.register(role='semantic',kwargs=foreign)
        with Scheduler(self.model.scheduler_db) as s,s._transaction() as cur:
            cur.execute('DROP TRIGGER '+SCOPES+'_update_immutable')
            cur.execute('UPDATE '+SCOPES+' SET content_hash=?',('0'*64,))
        second=self.wrapper();self.assertIn('hash differs',second.macro_error)
        with self.assertRaisesRegex(CockpitModelError,'fee_audit_required'):second.call(**self.kw(0))
        self.assertEqual(self.adapter.served,[])

    def test_real_run_memo_registers_all_groups_and_free_restarts_its_single_paid_repair(self):
        import json
        from dataclasses import replace
        from types import SimpleNamespace
        from tests import test_investment_memo_group_early_validation as early
        from dalton_core.investment_memo_cli import run_memo
        case=early.EarlyMemoValidationTests()
        frozen={**case.frozen(),'mission':self.mission}
        execute=self.adapter.execute
        def bad_answer(work,route,profile):
            invocation,envelope=execute(work,route,profile)
            return invocation,replace(envelope,outputs={'text':json.dumps(case.bad(),ensure_ascii=False)})
        self.adapter.execute=bad_answer
        def run():
            budget=MemoRepairBudget(self.model,run_micros=84_000_000,
                producer_micros=14_000_000,verifier_micros=14_000_000)
            result=run_memo(store=SimpleNamespace(),frozen=frozen,model=budget,
                verifier_model=SimpleNamespace(),max_repairs=1,repair_budget=budget)
            return result,budget
        result,first=run()
        self.assertEqual(result['status'],'refused')
        self.assertEqual(len(result['findings']),2)
        self.assertEqual(len(result['repairs']),1)
        self.assertEqual(len(self.adapter.served),2)
        with Scheduler(self.model.scheduler_db) as scheduler:
            self.assertEqual(scheduler.connection.execute('SELECT count(*) FROM '+REQUESTS).fetchone()[0],5)
            self.assertEqual(scheduler.connection.execute('SELECT count(*) FROM '+EXTRA).fetchone()[0],1)
        fees=self.fees()
        free,second=run()
        self.assertEqual(free['status'],'refused')
        self.assertEqual(free['cost_micros'],0)
        self.assertEqual(second.accounted_micros,first.accounted_micros)
        self.assertEqual(self.fees(),fees)
        self.assertEqual(len(self.adapter.served),2)


if __name__=='__main__':unittest.main()
