import json
import unittest
from dataclasses import replace
from datetime import timedelta
from unittest.mock import patch
from tests import test_deep_insight_gate_accounting as fixture
from dalton_core.deep_insight_gate_accounting import DeepGateAccounting,normal_request_id
from dalton_core.deep_insight_gate_execution import GateExecution,GateExecutionStopped
from dalton_core.deep_insight_gate_draft import build_group_prompt
from dalton_core.deep_insight_gate import GROUPS,GROUP_QUESTIONS
from dalton_core.shared_call_budget_policy import LIMITS_SCHEMA_VERSION,updated_shared_limits
from dalton_core.store import content_hash
from dalton_core.cockpit_model import CockpitModelError
from dalton_core.scheduler import Scheduler


class GateHumanGrantTests(unittest.TestCase):
    def setUp(self):
        fixture.DeepGateAccountingTests.setUp(self)
        timestamp=patch('dalton_core.thesis_impact_budget._utc',side_effect=lambda _:self.clock().isoformat())
        timestamp.start();self.addCleanup(timestamp.stop)
        def archived(model,ref):
            history=getattr(self,'history',[])+[self.old_policy]
            return next(p for p in history if p['content_hash']==ref)
        archive=patch('dalton_core.deep_insight_gate_budget_grant.load_archived_policy',side_effect=archived)
        archive.start();self.addCleanup(archive.stop)
        current=patch('dalton_core.deep_insight_gate_budget_grant.current_policy',side_effect=lambda _:self.history[0])
        current.start();self.addCleanup(current.stop)

    def setup_source(self,company):
        self.model.config['purpose_call_budgets']['deep_insight_gate'].update(
            max_input_tokens=120000,max_output_tokens=500,max_cost_usd=0.5,timeout_seconds=180)
        self.inputs={g:dict(questions={q:'Explain '+q for q in GROUP_QUESTIONS[g]},
             company={'company_ref':company},material=[],notes=[],prior_answers={},review=None)for g in GROUPS}
        self.prompts={g:build_group_prompt(group=g,**fields)for g,fields in self.inputs.items()}
        self.company=company
        self.model.config['purpose_run_budgets']={'deep_insight_gate':{'max_cost_usd':3,'max_units':4}}
        old=dict(schema_version=LIMITS_SCHEMA_VERSION,default_max_cost_usd=0.5,purpose_max_cost_usd={},
            purpose_call_limits={p:{'max_input_tokens':120000,'max_output_tokens':500,'timeout_seconds':180}
                                for p in ('deep_insight_gate','deep_insight_gate_verifier')},
            purpose_run_limits={'deep_insight_gate':{'max_cost_usd':3,'max_units':4}},
            actor_ref='human:test',revision=1,prior_hash=None,
            updated_at=(self.clock()-timedelta(seconds=10)).isoformat())
        old={**old,'content_hash':content_hash(old)}
        self.old_policy=old
        return old

    def kw(self,g):
        return dict(purpose='deep_insight_gate',request_id=normal_request_id(g,self.company,self.prompts[g]),
                    prompt=self.prompts[g],mission=self.mission)

    def old_round(self,fail_groups=('thesis',)):
        from tests.test_cockpit_model_fallback import ChainAdapter
        class Answered(ChainAdapter):
            def execute(inner,work,route,profile):
                invocation,result=super().execute(work,route,profile)
                if result.status=='succeeded':
                    g=next(g for g in GROUPS if work.question==self.prompts[g])
                    body={'answers':[{'question_ref':q,'status':'unknown','missing':'Missing primary evidence',
                        'evidence_that_would_answer':'A filed statement','gaps':[]}for q in GROUP_QUESTIONS[g]]}
                    if g=='industry':body['classification']='contract_compounder'
                    result=replace(result,outputs={'text':json.dumps(body)})
                return invocation,result
        self.adapter=Answered({});self.model.adapter_factory=lambda _:self.adapter
        for g in GROUPS:
            if g not in fail_groups:
                self.adapter.script={};self.model.call(**self.kw(g));continue
            self.adapter.script={'profile:gpt-6-astra':{'code':'TIMEOUT','message':'host timeout'}}
            with self.assertRaises(CockpitModelError):self.model.call(**self.kw(g))
            self.clock.advance(61)
            with self.assertRaises(CockpitModelError):self.model.call(**self.kw(g))
        self.clock.advance(1)

    def increase(self):
        p=updated_shared_limits(self.old_policy,purpose='deep_insight_gate',kind='run',
            budget={'max_cost_usd':6},actor_ref='human:test',updated_at=self.clock().isoformat())
        latest=updated_shared_limits(p,purpose='deep_insight_gate',kind='call',
            budget={'timeout_seconds':300,'max_output_tokens':600,'max_cost_usd':0.6},
            actor_ref='human:test',updated_at=self.clock().isoformat())
        self.model.config['purpose_call_budgets']['deep_insight_gate'].update(
            timeout_seconds=300,max_output_tokens=600,max_cost_usd=0.6)
        self.model.config['purpose_run_budgets']['deep_insight_gate']['max_cost_usd']=6
        self.history=[latest,p,self.old_policy]

    def account(self):
        a=DeepGateAccounting(self.model,self.prompts,company_ref=self.company)
        a.bind_consumer(self.inputs,wanted=GROUPS)
        with patch('dalton_core.deep_insight_gate_budget_grant.policy_history',return_value=self.history):a.import_legacy()
        return a

    def test_two_companies_old_success_free_and_one_human_timeout_child_retains_old_fees(self):
        for company in ('company:alpha','company:beta'):
            self.setup_source(company);self.old_round();old=fixture.fixture.MemoTimeoutRecoveryTests.fee_rows(self)
            self.increase();a=self.account();count=len(self.adapter.served)
            with patch('dalton_core.deep_insight_gate_budget_grant.policy_history',return_value=self.history):
                self.adapter.script={}
                e=GateExecution(self.model,a,run_micros=6000000,verifier_micros=500000)
                for g in GROUPS:e.stage('normal',g).call(**self.kw(g))
                self.assertEqual(len(self.adapter.served),count+1)
                self.assertTrue(a.last['cost_complete'])
                self.assertEqual(len(a.last['unknown_work_refs']),2)
                self.assertGreaterEqual(a.last['accounted_micros'],1000000)
                self.assertEqual(fixture.fixture.MemoTimeoutRecoveryTests.fee_rows(self)[0][:len(old[0])],old[0])
                self.assertEqual(fixture.fixture.MemoTimeoutRecoveryTests.fee_rows(self)[1][:len(old[1])],old[1])
                e.stage('normal','thesis').call(**self.kw('thesis'))
                self.assertEqual(len(self.adapter.served),count+1)

    def test_failed_grant_does_not_purchase_another_child_on_same_or_unrelated_revision(self):
        self.setup_source('company:alpha');self.old_round();self.increase();a=self.account()
        with patch('dalton_core.deep_insight_gate_budget_grant.policy_history',return_value=self.history):
            e=GateExecution(self.model,a,run_micros=6000000,verifier_micros=0).stage('normal','thesis')
            with self.assertRaises(GateExecutionStopped):e.call(**self.kw('thesis'))
            sent=len(self.adapter.served);old=fixture.fixture.MemoTimeoutRecoveryTests.fee_rows(self)
            for _ in range(2):
                with self.assertRaises(GateExecutionStopped):e.call(**self.kw('thesis'))
            unrelated=updated_shared_limits(self.history[0],purpose='ask',kind='call',budget={'max_cost_usd':2},
                actor_ref='human:test',updated_at=self.clock().isoformat())
            self.history.insert(0,unrelated)
            with self.assertRaises(GateExecutionStopped):e.call(**self.kw('thesis'))
            self.assertEqual(len(self.adapter.served),sent)
            self.assertEqual(fixture.fixture.MemoTimeoutRecoveryTests.fee_rows(self),old)

    def test_mismatched_consumer_and_missing_policy_do_not_authorize(self):
        self.setup_source('company:alpha');self.old_round();self.increase()
        a=DeepGateAccounting(self.model,self.prompts,company_ref=self.company)
        bad={**self.inputs,'thesis':{**self.inputs['thesis'],'notes':['foreign source']}}
        with self.assertRaisesRegex(ValueError,'prompt bytes'):a.bind_consumer(bad,wanted=GROUPS)
        a=self.account()
        with patch('dalton_core.deep_insight_gate_budget_grant.policy_history',return_value=[self.old_policy]):
            with self.assertRaisesRegex(ValueError,'has not increased'):a.recovery_grant(self.kw('thesis'),group='thesis')

    def test_two_timeout_groups_prepare_together_and_original_grants_survive_restart(self):
        self.setup_source('company:gamma');self.old_round(('market','thesis'));self.increase()
        a=self.account();count=len(self.adapter.served)
        with patch('dalton_core.deep_insight_gate_budget_grant.policy_history',return_value=self.history):
            self.adapter.script={}
            e=GateExecution(self.model,a,run_micros=6000000,verifier_micros=500000)
            e.stage('normal','market').call(**self.kw('market'))
            self.assertEqual(len(a.grants),2)
            restarted=self.account()
            e=GateExecution(self.model,restarted,run_micros=6000000,verifier_micros=500000)
            e.stage('normal','market').call(**self.kw('market'))
            e.stage('normal','thesis').call(**self.kw('thesis'))
            self.assertEqual(len(self.adapter.served),count+2)
            self.assertEqual(len(restarted.snapshot()[2]['unknown_work_refs']),4)

    def test_run_increase_alone_or_unknown_other_failure_never_authorizes(self):
        self.setup_source('company:alpha');self.old_round();self.increase();a=self.account()
        run_only=self.history[1]
        with patch('dalton_core.deep_insight_gate_budget_grant.policy_history',return_value=[run_only,self.old_policy]):
            with self.assertRaisesRegex(ValueError,'has not increased'):a.recovery_grant(self.kw('thesis'),group='thesis')
        count=len(self.adapter.served)
        with patch('dalton_core.deep_insight_gate_budget_grant.load_archived_policy',return_value={**self.old_policy,'default_max_cost_usd':99}):
            with self.assertRaisesRegex(ValueError,'archive'):a.snapshot()
        self.assertEqual(len(self.adapter.served),count)

    def test_imported_scope_uses_original_run_three_without_a_grant(self):
        self.setup_source('company:alpha');self.old_round();self.increase();a=self.account()
        p=a.snapshot()[2]
        self.assertEqual(a.legacy_run_budget['max_cost_usd'],3)
        self.assertFalse(a.grants)
        with self.assertRaisesRegex(ValueError,'systemic unknown'):
            a.before_paid(run_micros=6000000,next_call_micros=500000)

    def test_unrelated_growth_after_import_does_not_expand_or_recount_old_scope(self):
        self.setup_source('company:alpha');self.old_round();self.increase();a=self.account()
        with patch('dalton_core.deep_insight_gate_budget_grant.policy_history',return_value=self.history):
            self.adapter.script={}
            GateExecution(self.model,a,run_micros=6000000,verifier_micros=0).stage('normal','thesis').call(**self.kw('thesis'))
            prior=a.snapshot()[2]
            # Unrelated later verifier rows cannot enter the old bounded census.
            from dalton_core.contracts import WorkOrder
            with Scheduler(self.model.scheduler_db)as scheduler:
                original=next(row for row in scheduler.connection.execute('SELECT work_order_id FROM scheduler_work_orders'))[0]
                wire=scheduler.work_order_authority(original)['work_order']
                for n in range(130):
                    copied={**wire,'idempotency_key':'unrelated-verifier-'+str(n),'id':'work:cockpit-deep_insight_gate_verifier-'+format(n,'032x'),
                        'metadata':{**wire['metadata'],'purpose':'deep_insight_gate_verifier','request_id':'unrelated-'+str(n),
                                    'producer_route_decision_refs':['route:other']}}
                    self.assertEqual(scheduler.enqueue(WorkOrder.from_dict(copied))['status'],'fresh')
            self.assertEqual(a.snapshot()[2]['bills'],prior['bills'])
            self.assertTrue(a.snapshot()[2]['cost_complete'])

    def test_actual_related_increase_cannot_open_second_scope_group_grant(self):
        self.setup_source('company:alpha');self.old_round();self.increase();a=self.account()
        with patch('dalton_core.deep_insight_gate_budget_grant.policy_history',side_effect=lambda _:self.history):
            e=GateExecution(self.model,a,run_micros=6000000,verifier_micros=0).stage('normal','thesis')
            with self.assertRaises(GateExecutionStopped):e.call(**self.kw('thesis'))
            count=len(self.adapter.served);fees=fixture.fixture.MemoTimeoutRecoveryTests.fee_rows(self)
            increased=updated_shared_limits(self.history[0],purpose='deep_insight_gate',kind='call',
                budget={'timeout_seconds':600,'max_cost_usd':1},actor_ref='human:test',updated_at=self.clock().isoformat())
            self.history.insert(0,increased)
            with self.assertRaisesRegex(GateExecutionStopped,'one grant'):e.call(**self.kw('thesis'))
            self.assertEqual(len(self.adapter.served),count)
            self.assertEqual(fixture.fixture.MemoTimeoutRecoveryTests.fee_rows(self),fees)

    def test_linked_verifier_and_missing_formal_cannot_claim_normal_only_history(self):
        self.setup_source('company:alpha');self.old_round();self.increase()
        with Scheduler(self.model.scheduler_db)as scheduler:
            first=scheduler.connection.execute('SELECT work_order_id FROM scheduler_work_orders ORDER BY rowid LIMIT 1').fetchone()[0]
            wire=scheduler.work_order_authority(first)['work_order']
            selected=scheduler.formal_result(first)['result_envelope']['metadata']['route_decision_ref']
            from dalton_core.contracts import WorkOrder
            copy={**wire,'idempotency_key':'linked-verifier','id':'work:cockpit-deep_insight_gate_verifier-'+'f'*32,
                'metadata':{**wire['metadata'],'purpose':'deep_insight_gate_verifier','request_id':'verifier:linked',
                            'producer_route_decision_refs':[selected]}}
            self.assertEqual(scheduler.enqueue(WorkOrder.from_dict(copy))['status'],'fresh')
        a=DeepGateAccounting(self.model,self.prompts,company_ref=self.company)
        a.bind_consumer(self.inputs,wanted=GROUPS)
        with patch('dalton_core.deep_insight_gate_budget_grant.policy_history',return_value=self.history):
            with self.assertRaisesRegex(ValueError,'linked verifier'):a.import_legacy()
            with patch('dalton_core.scheduler.Scheduler.formal_result',return_value=None):
                with self.assertRaisesRegex(ValueError,'in flight'):a.import_legacy()

    def test_original_failed_shape_is_not_guessed_to_have_zero_extra_fees(self):
        self.setup_source('company:alpha')
        # A real paid successful envelope whose body needs shape repair.
        self.model.call(**self.kw('industry'))
        self.adapter.script={'profile:gpt-6-astra':{'code':'TIMEOUT','message':'host timeout'}}
        with self.assertRaises(CockpitModelError):self.model.call(**self.kw('thesis'))
        self.clock.advance(61);self.increase()
        a=DeepGateAccounting(self.model,self.prompts,company_ref=self.company)
        a.bind_consumer(self.inputs,wanted=GROUPS)
        fees=fixture.fixture.MemoTimeoutRecoveryTests.fee_rows(self)
        with self.assertRaisesRegex(ValueError,'anonymous shape repair'):a.import_legacy()
        self.assertFalse(a.snapshot()[2]['cost_complete'])
        self.assertEqual(fixture.fixture.MemoTimeoutRecoveryTests.fee_rows(self),fees)

    def test_terminal_unknown_can_pause_only_after_full_accounting_and_exact_grant(self):
        import hashlib,os
        from dalton_core.deep_insight_gate_quiesce import RunQuiesce,request_quiesce,GateQuiesced
        self.setup_source('company:alpha');self.old_round();self.increase();a=self.account()
        with patch('dalton_core.deep_insight_gate_budget_grant.policy_history',return_value=self.history):
            a.recovery_grant(self.kw('thesis'),group='thesis')
            directory=self.root/'pause';directory.mkdir()
            producer=self.root/'p.json';producer.write_bytes(b'{}')
            verifier=self.root/'v.json';verifier.write_bytes(b'{}')
            ticket=dict(id='deep-insight-gate-run:'+'a'*24,pid=os.getpid(),started_at=self.clock().isoformat(),
                command=['python','-m','dalton_core.deep_insight_gate_cli'],company_ref=self.company,
                source_fingerprint='f'*64,model_config_path=str(producer),verifier_model_config_path=str(verifier),
                model_config_fingerprint=hashlib.sha256(b'{}').hexdigest(),
                verifier_model_config_fingerprint=hashlib.sha256(b'{}').hexdigest(),
                status='running',exit_code=None,completed_at=None)
            (directory/'ticket.json').write_text(json.dumps(ticket))
            with patch('dalton_core.lane_child_launcher.ticket_identity',return_value=True):
                request_quiesce(directory,expected_ticket_hash=content_hash(ticket))
            control=RunQuiesce(directory,producer,verifier,producer_bytes=b'{}',verifier_bytes=b'{}',
                accounting=a,company_ref=self.company,source_fingerprint='f'*64)
            fees=fixture.fixture.MemoTimeoutRecoveryTests.fee_rows(self);count=len(self.adapter.served)
            with self.assertRaises(GateQuiesced):control.checkpoint()
            self.assertEqual(len(a.last['unknown_work_refs']),2)
            self.assertEqual(fixture.fixture.MemoTimeoutRecoveryTests.fee_rows(self),fees)
            self.assertEqual(len(self.adapter.served),count)
