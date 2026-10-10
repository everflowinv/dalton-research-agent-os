"""Actual paid failure -> exact hold -> new Work fallback, without budget edits."""
import copy
import json
import unittest
from dataclasses import replace
from datetime import datetime, timedelta
from unittest.mock import patch

from tests import test_cockpit_model_fallback as fixture
from dalton_core.cockpit_model import CockpitModelError
from dalton_core.model_router import ModelRouter
from dalton_core.scheduler import Scheduler
from dalton_core.contracts import InvocationGranularity
from dalton_core.store import content_hash
from dalton_core.thesis_impact_budget import ThesisImpactBudgetStore
from dalton_core.model_output_contract_hold import HOLD_SECONDS, SKIP_REASON, record

class PaidOverrun(fixture.ChainAdapter):
    def __init__(self, *, usd=.6, variant=None):
        super().__init__({})
        self.usd, self.variant, self.proofs = usd, variant, []
    def execute(self, work, route, profile):
        inv, result = super().execute(work, route, profile)
        if profile['id']!='profile:gpt-6-astra':
            return replace(inv, usage={'raw_provider_telemetry':{'cost':{'available':True,'usd':.001}}}), result
        digest=content_hash({'synthetic_response':work.id})
        usage={'thinking_level':route['thinking_level'],'output_tokens':work.budget['max_output_tokens']+100,
               'metering_source':'provider_reported','raw_provider_telemetry':{'cost':{'available':True,'usd':self.usd},'response_content_hash':digest}}
        metadata={'route_decision_ref':route['id'],'profile_version_ref':profile['profile_version_ref'],
                  'thinking_level':route['thinking_level'],'broker_response_hash':digest,
                  'runtime_version':'synthetic-host:1','broker_version':'synthetic-broker:1','dispatch_proof':None}
        if self.variant=='missing_usage':usage={}
        if self.variant=='unknown_dispatch':metadata['dispatch_proof']={'state':'unknown'}
        if self.variant=='missing_cost':usage['raw_provider_telemetry']['cost']={'available':False,'usd':None}
        inv=replace(inv,runtime_ref=profile['adapter_ref'],actor_ref='runtime:openclaw-model-broker',granularity=InvocationGranularity.WORK_ORDER,created_at=route['created_at'],completed_at=route['created_at'],usage=usage,parent_ref=route['id'])
        result=replace(result,created_at=route['created_at'],status='failed',outputs={},error={
            'code':'PROVIDER_BUDGET_EXCEEDED','source':'openclaw-model-adapter',
            'message':'provider max_output_tokens telemetry exceeds WorkOrder budget'},metadata=metadata)
        if self.variant=='other_error':result=replace(result,error={**result.error,'message':'provider cost telemetry exceeds WorkOrder budget'})
        self.proofs.append((work,route,profile,inv,result))
        return inv,result

class ExactOutputHoldTests(unittest.TestCase):
    def setUp(self):
        fixture.CockpitChainTests.setUp(self)
        self.budget_authority=ThesisImpactBudgetStore(self.root/'budget.sqlite')
        self.addCleanup(self.budget_authority.close)
    _model=fixture.CockpitChainTests._model
    def fail(self, adapter, *, policy=None, key='first'):
        model=self._model(adapter,policy_version_ref=policy or self.chain_policy)
        with self.assertRaises(CockpitModelError):
            model.call(purpose='plan',request_id=key,prompt='synthetic plan',mission=self.mission)
        return model
    def holds(self):
        with ModelRouter(self.router_db,read_only=True) as router:
            return [json.loads(r[0]) for r in router.connection.execute('SELECT record_json FROM model_output_contract_holds')]
    def test_paid_chain_overrun_falls_back_on_new_work_and_preserves_full_fee(self):
        adapter=PaidOverrun();model=self.fail(adapter)
        self.assertEqual(adapter.served,['profile:gpt-6-astra'])
        self.assertEqual(len(self.holds()),1)
        answer=model.call(purpose='plan',request_id='new-material',prompt='new synthetic plan',mission=self.mission)
        self.assertIn('claude',answer['text']);self.assertEqual(adapter.served,['profile:gpt-6-astra','profile:claude-fable-5-1'])
        with ThesisImpactBudgetStore(self.root/'budget.sqlite') as budget:
            paid=[r[0] for r in budget.connection.execute('SELECT actual_micros FROM thesis_impact_day_settlements ORDER BY created_at')]
        self.assertEqual(paid,[600000,1000]) # Actual over reservation remains fully booked.
        with ModelRouter(self.router_db,read_only=True) as router:
            latest=router.list_decisions()[-1]
            first=next(p for p in latest['candidate_snapshot'] if p['profile_version_ref']==adapter.proofs[0][2]['profile_version_ref'])
            self.assertIn(SKIP_REASON,first['rejection_reasons'])
            self.assertEqual(router.active_cooldowns(),{})
        calls=len(adapter.served)
        with self.assertRaises(CockpitModelError):model.call(purpose='plan',request_id='first',prompt='synthetic plan',mission=self.mission)
        self.assertEqual(len(adapter.served),calls) # Old failed Work is never reexecuted.
    def test_investment_memo_is_not_held_or_globally_retired(self):
        adapter=PaidOverrun();model=self.fail(adapter)
        adapter.variant='missing_usage'
        model.call(purpose='investment_memo',request_id='memo',prompt='synthetic memo',mission=self.mission)
        self.assertEqual(adapter.served[1],'profile:gpt-6-astra')
        self.assertEqual(len(self.holds()),1)
    def test_exact_caps_change_and_policy_revision_invalidate_hold(self):
        adapter=PaidOverrun();model=self.fail(adapter)
        model.config['purpose_call_budgets']={'plan':{'max_output_tokens':model.budget_for('plan')['max_output_tokens']+1}}
        with self.assertRaises(CockpitModelError):model.call(purpose='plan',request_id='new-cap',prompt='new cap',mission=self.mission)
        self.assertEqual(adapter.served[-1],'profile:gpt-6-astra')
        with ModelRouter(self.router_db) as router:
            old=router.get_policy(self.chain_policy);changed={k:v for k,v in old.items() if k not in {'policy_version_ref','content_hash'}}
            changed['created_at']=router._now().isoformat();changed['version']+=1;changed['prior_version_ref']=old['policy_version_ref'];changed['policy_version_ref']=old['policy_version_ref']+'-revised'
            self.assertEqual(router.register_policy(changed)['status'],'fresh');ref=changed['policy_version_ref']
        model=self._model(adapter,policy_version_ref=ref)
        with self.assertRaises(CockpitModelError):model.call(purpose='plan',request_id='new-policy',prompt='new policy',mission=self.mission)
        self.assertEqual(adapter.served[-1],'profile:gpt-6-astra')
    def test_hour_expiry_does_not_rewrite_old_hold(self):
        adapter=PaidOverrun();model=self.fail(adapter)
        old=self.holds()[0];expires=datetime.fromisoformat(old['until'])
        self.assertEqual((expires-datetime.fromisoformat(old['observed_at'])).total_seconds(),HOLD_SECONDS)
        with patch.object(ModelRouter,'_now',return_value=expires+timedelta(seconds=1)):
            with self.assertRaises(CockpitModelError):model.call(purpose='plan',request_id='hour-later',prompt='after expiry',mission=self.mission)
        self.assertEqual(adapter.served,['profile:gpt-6-astra','profile:gpt-6-astra'])
        self.assertEqual(self.holds()[0],old)
    def test_missing_unknown_or_non_output_failure_does_not_trigger_hold(self):
        for variant in ['missing_usage','missing_cost','unknown_dispatch','other_error']:
            with self.subTest(variant=variant):
                adapter=PaidOverrun(variant=variant);model=self._model(adapter,policy_version_ref=self.chain_policy)
                try:model.call(purpose='plan',request_id=variant,prompt='negative proof',mission=self.mission)
                except CockpitModelError:pass
                self.assertEqual(adapter.served[0],'profile:gpt-6-astra')
        self.assertEqual(self.holds(),[])
    def test_direct_paid_failure_is_captured_only_after_settlement(self):
        adapter=PaidOverrun();model=self.fail(adapter,policy=self.pinned_policy)
        hold=self.holds()[0]
        self.assertEqual(hold['proof']['actual_cost_micros'],600000)
        with ThesisImpactBudgetStore(self.root/'budget.sqlite') as budget:
            paid=budget.admission(work_order_ref=adapter.proofs[0][0].id,attempt_number=1,phase='assessment')
        self.assertEqual(paid['settlement']['content_hash'],hold['proof']['settlement_hash'])
    def test_replay_idempotency_and_tampered_scope_are_refused(self):
        adapter=PaidOverrun();self.fail(adapter);work,route,profile,inv,result=adapter.proofs[0]
        with ModelRouter(self.router_db) as router,ThesisImpactBudgetStore(self.root/'budget.sqlite') as budget:
            paid=budget.admission(work_order_ref=work.id,attempt_number=1,phase='assessment')
            args=dict(work=work,route=route,profile=profile,invocation=inv,result=result,budget_store=budget,admission_id=paid['admission']['admission_id'])
            before=self.holds();record(router,**args);self.assertEqual(self.holds(),before)
            forged=replace(inv,work_order_ref='work:wrong')
            with self.assertRaises(ValueError):record(router,**{**args,'invocation':forged})
        self.assertEqual(len(self.holds()),1)
    def test_thinking_override_and_profile_revision_do_not_match_prior_hold(self):
        adapter=PaidOverrun();self.fail(adapter)
        proof=adapter.proofs[0];work,route,profile=proof[:3]
        from dalton_core.model_output_contract_hold import active
        with ModelRouter(self.router_db,read_only=True) as router:
            args=dict(policy_ref=route['policy_version_ref'],policy_hash=route['policy_hash'],purpose='plan',budget=work.budget,thinking_level=route['thinking_level'],now=router._now())
            self.assertIsNotNone(active(router.connection,profile,**args))
            self.assertIsNone(active(router.connection,profile,**{**args,'thinking_level':'high'}))
            self.assertIsNone(active(router.connection,{**profile,'profile_version_ref':profile['profile_version_ref']+'-new'},**args))
            self.assertIsNone(active(router.connection,profile,**{**args,'purpose':'investment_memo'}))
    def test_old_snapshot_without_hold_table_is_readable_without_ddl(self):
        import sqlite3
        from dalton_core.model_output_contract_hold import active
        connection=sqlite3.connect(':memory:')
        connection.execute('PRAGMA query_only=ON')
        self.assertIsNone(active(connection,{},policy_ref='unused',policy_hash='unused',purpose='plan',budget={},thinking_level='low',now=datetime.now().astimezone()))
        self.assertEqual(connection.execute('SELECT count(*) FROM sqlite_master').fetchone()[0],0)
        connection.close()
    def test_seed_preview_is_read_only_and_original_expiry_is_not_extended(self):
        adapter=PaidOverrun();self.fail(adapter);work,route,profile,inv,result=adapter.proofs[0]
        with ModelRouter(self.router_db,read_only=True) as router,ThesisImpactBudgetStore(self.root/'budget.sqlite',read_only=True) as budget:
            paid=budget.admission(work_order_ref=work.id,attempt_number=1,phase='assessment')
            args=dict(work=work,route=route,profile=profile,invocation=inv,result=result,budget_store=budget,admission_id=paid['admission']['admission_id'],preview=True)
            before=self.holds();preview=record(router,**args);self.assertEqual(preview,before[0])
            expiry=datetime.fromisoformat(preview['until'])
            with patch.object(ModelRouter,'_now',return_value=expiry+timedelta(seconds=1)):
                self.assertIsNone(record(router,**args))
            self.assertEqual(self.holds(),before)
