"""A proved successful group retains earlier TIMEOUT charges without a new slot."""
import unittest
import json
from dataclasses import replace
from unittest.mock import patch
from tests import test_deep_insight_gate_budget_grant as fixture
from tests import test_deep_insight_gate_accounting as accounts
from dalton_core.deep_insight_gate_execution import GateExecution, GateExecutionStopped
from dalton_core.deep_insight_gate import GROUPS
from dalton_core.cockpit_model import CockpitModelError


class CompletedTimeoutFeesTests(unittest.TestCase):
    setUp = fixture.GateHumanGrantTests.setUp
    setup_source = fixture.GateHumanGrantTests.setup_source
    kw = fixture.GateHumanGrantTests.kw
    old_round = fixture.GateHumanGrantTests.old_round
    increase = fixture.GateHumanGrantTests.increase
    account = fixture.GateHumanGrantTests.account

    def ready(self, company='company:alpha', *, malformed_company=False):
        self.setup_source(company)
        from tests.test_cockpit_model_fallback import ChainAdapter
        from dalton_core.deep_insight_gate import GROUP_QUESTIONS
        class Answered(ChainAdapter):
            def execute(inner, work, route, profile):
                invocation, result = super().execute(work, route, profile)
                if result.status == 'succeeded':
                    group = next(g for g in GROUPS if work.question == self.prompts[g])
                    body = {'answers':[{'question_ref':q,'status':'unknown','missing':'Missing primary evidence',
                        'evidence_that_would_answer':'A filed statement','gaps':[]} for q in GROUP_QUESTIONS[group]]}
                    if group == 'industry': body['classification'] = 'contract_compounder'
                    result = replace(result, outputs={'text':json.dumps({'invented':True} if malformed_company and group=='company' else body)})
                return invocation, result
        self.adapter = Answered({}); self.model.adapter_factory = lambda _:self.adapter
        for group in GROUPS:
            self.adapter.script = {}
            if group in ('company', 'thesis'):
                self.adapter.script = {'profile:gpt-6-astra':{'code':'TIMEOUT','message':'host timeout'}}
                with self.assertRaises(CockpitModelError): self.model.call(**self.kw(group))
                self.clock.advance(61)
                if group == 'company': self.adapter.script = {}
            if group == 'thesis':
                with self.assertRaises(CockpitModelError): self.model.call(**self.kw(group))
            else: self.model.call(**self.kw(group))
        self.clock.advance(1)
        self.increase()
        self.adapter.script = {}
        return self.account()

    def test_two_companies_successful_group_old_timeouts_do_not_block_other_granted_step(self):
        for company in ('company:alpha', 'company:beta'):
            a = self.ready(company)
            fees = accounts.fixture.MemoTimeoutRecoveryTests.fee_rows(self)
            count = len(self.adapter.served)
            with patch('dalton_core.deep_insight_gate_budget_grant.policy_history', return_value=self.history):
                run = GateExecution(self.model, a, run_micros=6000000, verifier_micros=500000)
                for group in GROUPS:
                    run.stage('normal', group).call(**self.kw(group))
                self.assertEqual(len(self.adapter.served), count + 1)
                self.assertEqual(set(a.grants), {'thesis'})
                self.assertEqual(len(a.last['unknown_work_refs']), 3)
                a.before_paid(run_micros=6000000, next_call_micros=600000, mission=self.mission)
                self.assertEqual(sum(p['group']=='company' for p in a.last['completed_group_retained_timeout_proofs']), 1)
                current = accounts.fixture.MemoTimeoutRecoveryTests.fee_rows(self)
                self.assertEqual(current[0][:len(fees[0])], fees[0])
                self.assertEqual(current[1][:len(fees[1])], fees[1])
                run.stage('normal', 'company').call(**self.kw('company'))
                self.assertEqual(len(self.adapter.served), count + 1)

    def test_paid_success_envelope_with_invalid_body_does_not_prove_a_completed_group(self):
        with self.assertRaisesRegex(ValueError, 'anonymous shape repair|unproved prior shape'):
            self.ready(malformed_company=True)

    def test_mission_mismatch_and_unproved_success_cannot_clear_unknown(self):
        a = self.ready()
        with patch('dalton_core.deep_insight_gate_budget_grant.policy_history', return_value=self.history):
            a.recovery_grant(self.kw('thesis'), group='thesis')
            with self.assertRaisesRegex(ValueError, 'systemic unknown'):
                a.before_paid(run_micros=6000000, next_call_micros=600000,
                              mission={**self.mission, 'content_hash':'f'*64})
            with patch('dalton_core.deep_insight_gate_accounting.works', side_effect=lambda c,e,**kw:
                       [(w, auth, None) for w,auth,formal in original(c,e,**kw)]):
                with self.assertRaisesRegex(ValueError, 'in-flight'):
                    a.before_paid(run_micros=6000000, next_call_micros=600000, mission=self.mission)

    def test_non_timeout_unknown_is_not_accepted_by_success_proof(self):
        a = self.ready()
        with patch('dalton_core.deep_insight_gate_budget_grant.policy_history', return_value=self.history):
            a.recovery_grant(self.kw('thesis'), group='thesis')
            from dalton_core.deep_insight_gate_budget_grant import grant_proof
            def only_thesis(**kwargs):
                if kwargs['entry']['group']=='company':
                    raise ValueError('not a pure TIMEOUT terminal')
                return grant_proof(**kwargs)
            with patch('dalton_core.deep_insight_gate_budget_grant.grant_proof', side_effect=only_thesis):
                with self.assertRaisesRegex(ValueError, 'systemic unknown'):
                    a.before_paid(run_micros=6000000, next_call_micros=600000, mission=self.mission)

    def test_source_change_or_new_unknown_cannot_borrow_completed_group_proof(self):
        a = self.ready()
        with patch('dalton_core.deep_insight_gate_budget_grant.policy_history', return_value=self.history):
            a.recovery_grant(self.kw('thesis'), group='thesis')
            a.consumer = {**a.consumer, 'group_inputs':{**a.consumer['group_inputs'],
                         'company':{**a.consumer['group_inputs']['company'], 'material':[{'ref':'foreign'}]}}}
            with self.assertRaisesRegex(ValueError, 'completion authority|current consumer'):
                a.before_paid(run_micros=6000000, next_call_micros=600000, mission=self.mission)

    def test_new_correction_unknown_is_not_covered_by_older_completed_group(self):
        a = self.ready()
        with patch('dalton_core.deep_insight_gate_budget_grant.policy_history', return_value=self.history):
            a.recovery_grant(self.kw('thesis'), group='thesis')
            kwargs = {**self.kw('industry'), 'request_id':'new-numeric-repair', 'prompt':'A new correction'}
            a.register(role='numbers', group='industry', kwargs=kwargs)
            self.adapter.script = {'profile:gpt-6-astra':{'code':'TIMEOUT','message':'host timeout'}}
            with self.assertRaises(CockpitModelError): self.model.call(**kwargs)
            count = len(self.adapter.served)
            fees = accounts.fixture.MemoTimeoutRecoveryTests.fee_rows(self)
            with self.assertRaisesRegex(ValueError, 'systemic unknown'):
                a.before_paid(run_micros=6000000, next_call_micros=600000, mission=self.mission)
            self.assertEqual(len(self.adapter.served),count)
            self.assertEqual(accounts.fixture.MemoTimeoutRecoveryTests.fee_rows(self),fees)

    def test_no_mission_cannot_enable_completed_group_exception(self):
        a = self.ready()
        with patch('dalton_core.deep_insight_gate_budget_grant.policy_history', return_value=self.history):
            a.recovery_grant(self.kw('thesis'), group='thesis')
            with self.assertRaisesRegex(ValueError, 'systemic unknown'):
                a.before_paid(run_micros=6000000, next_call_micros=600000)


from dalton_core.deep_insight_gate_accounting import works as original
