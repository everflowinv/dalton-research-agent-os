"""Actual extraction accounting -> paid output hold -> next ordinary window."""
import copy
import json
import sqlite3
import unittest
from dataclasses import replace
from unittest.mock import patch

from tests import test_document_extraction_admission as fixture
from dalton_core.contracts import InvocationGranularity
from dalton_core.document_extraction import HermeticExtractionAdapter
from dalton_core.model_router import ModelRouter
from dalton_core.model_output_contract_hold import SKIP_REASON
from dalton_core.openclaw_model_adapter import OpenClawModelAdapter
from dalton_core.store import content_hash
from dalton_core.thesis_impact_budget import ThesisImpactBudgetStore


class ExtractionOutputHoldTests(unittest.TestCase):
    def setUp(self):
        self.f = fixture.BrokerAdmissionTests('test_broker_composition_reserves_then_accounts_once_and_replays')
        self.f.setUp()
        self.addCleanup(self.f.doCleanups)
        f = self.f
        self.clock_patch = patch.object(ModelRouter, '_now', side_effect=f.h.h.clock)
        self.clock_patch.start(); self.addCleanup(self.clock_patch.stop)
        backup = copy.deepcopy(f.pr)
        backup.update(profile_version_ref='model-profile-version:synthetic-backup:1',
                      id='profile:synthetic-backup', model='synthetic-backup', family='synthetic-backup',
                      credential_slot_ref='credential-slot:synthetic-backup')
        f.router.register_profile(backup)
        self.backup = backup
        pol = fixture.policy()
        pol.update(policy_version_ref='model-routing-policy-version:synthetic-extraction-chain:1',
                   id='model-routing-policy:synthetic-extraction-chain',
                   purpose_overrides={'document_extraction':{'mode':'explicit','chain':[f.pr['id'],backup['id']]}})
        f.router.register_policy(pol)
        config = f.h.writer._document_extraction_model_config
        config['routing_policy_ref'] = pol['policy_version_ref']
        config['credential_slot_refs'] = [f.pr['credential_slot_ref'],backup['credential_slot_ref']]
        self.served=[];self.variant=None

    def respond(self, adapter, work, route, profile, *, before_send=None):
        if before_send: before_send()
        self.served.append(profile['id'])
        context=work.metadata['context']
        wire={'schema_version':'0.1','suggestions':[{'quote_id':context['quotes'][0]['quote_id'],
              'normalized_statement':'Management described cautious client decisions.',
              'metric_or_aspect':'aspect:decisions','period':'current commentary','basis':'management'}]}
        fp=copy.deepcopy(profile);fp['provider']='hermetic-fixture'
        fp['cost']['input_per_million_usd']=fp['cost']['output_per_million_usd']=0
        inv,res=HermeticExtractionAdapter(wire,created_at=route['created_at']).execute(work,route,fp)
        digest=content_hash({'synthetic_response':work.id})
        overrun=profile['id']==self.f.pr['id']
        use={'thinking_level':route['thinking_level'],'input_tokens':10,
             'output_tokens':work.budget['max_output_tokens']+10 if overrun else 10,
             'total_tokens':work.budget['max_output_tokens']+20 if overrun else 20,
             'metering_source':'provider_reported',
             'raw_provider_telemetry':{'cost':{'available':True,'usd':.001},'response_content_hash':digest}}
        metadata={'route_decision_ref':route['id'],'profile_version_ref':profile['profile_version_ref'],
                  'thinking_level':route['thinking_level'],'broker_response_hash':digest,
                  'runtime_version':'synthetic-host:1','broker_version':'synthetic-broker:1','dispatch_proof':None}
        error={'code':'PROVIDER_BUDGET_EXCEEDED','source':'openclaw-model-adapter',
               'message':'provider max_output_tokens telemetry exceeds WorkOrder budget'}
        if self.variant=='input': error['message']='provider max_input_tokens telemetry exceeds WorkOrder budget'
        if self.variant=='cost': error['message']='provider cost telemetry exceeds WorkOrder budget'
        if self.variant=='unknown':metadata['dispatch_proof']={'state':'unknown'}
        if self.variant=='missing_cost':use['raw_provider_telemetry']['cost']={'available':False,'usd':None}
        inv=replace(inv,provider=profile['provider'],runtime_ref=profile['adapter_ref'],
                    actor_ref='runtime:openclaw-model-broker',granularity=InvocationGranularity.WORK_ORDER,
                    model_family=profile['family'],usage=use)
        res=replace(res,status='failed' if overrun else 'succeeded',error=error if overrun else None,
                    outputs={} if overrun else res.outputs,metadata=metadata)
        return inv,res

    def generate(self, offset=0):
        self.f.h.params['offset']=offset
        with patch.object(OpenClawModelAdapter,'execute',autospec=True,side_effect=self.respond):
            return self.f.h.generate()

    def holds(self):
        return [json.loads(r[0]) for r in self.f.router.connection.execute('SELECT record_json FROM model_output_contract_holds')]

    def test_paid_failure_next_normal_window_uses_owner_next_profile_without_same_window_retry(self):
        first=self.generate();self.assertEqual(first['status'],'failed')
        self.assertEqual(self.served,[self.f.pr['id']]);holds=self.holds()
        self.assertEqual(len(holds),1);self.assertEqual(holds[0]['scope']['purpose'],'document_extraction')
        old_work=self.f.h.h.scheduler.work_order_authority(first['work_order_ref'])['work_order']
        self.assertNotIn('purpose',old_work['metadata'])
        replay=self.generate();self.assertEqual(replay['status'],'failed')
        self.assertEqual(self.served,[self.f.pr['id']]);self.assertEqual(self.holds(),holds)
        next_result=self.generate(fixture.WINDOW_CHARS)
        self.assertEqual(next_result['status'],'succeeded',next_result)
        self.assertEqual(self.served,[self.f.pr['id'],self.backup['id']])
        paid=self.f.b.connection.execute('SELECT actual_micros FROM thesis_impact_day_settlements').fetchall()
        self.assertEqual([r[0] for r in paid],[1000,1000])
        decision=self.f.router.list_decisions(work_order_id=next_result['work_order_ref'])[-1]
        candidate=next(c for c in decision['candidate_snapshot'] if c['profile_version_ref']==self.f.pr['profile_version_ref'])
        self.assertIn(SKIP_REASON,candidate['rejection_reasons'])
        self.assertEqual(self.f.router.active_cooldowns(),{})

    def assert_no_hold(self, variant):
        self.variant=variant
        self.generate()
        self.assertEqual(self.holds(),[])
        self.assertEqual(self.served,[self.f.pr['id']])

    def test_input_overrun_never_holds(self):
        self.assert_no_hold('input')

    def test_cost_overrun_never_holds(self):
        self.assert_no_hold('cost')

    def test_unknown_dispatch_never_holds(self):
        self.assert_no_hold('unknown')

    def test_missing_actual_cost_never_holds(self):
        self.assert_no_hold('missing_cost')

    def test_deferred_settlement_never_claims_hold_and_replay_does_not_call_provider(self):
        from dalton_core import document_extraction_settlement as settlement
        with patch.object(settlement,'SETTLEMENT_RETRY_SECONDS',0),patch.object(
                ThesisImpactBudgetStore,'settle',side_effect=sqlite3.OperationalError('database is locked')):
            result=self.generate()
        self.assertEqual(result['status'],'failed');self.assertEqual(self.holds(),[])
        self.assertEqual(self.f.b.connection.execute('SELECT count(*) FROM thesis_impact_day_settlements').fetchone()[0],0)
        from dalton_core.pending_settlement_registry import PendingSettlements
        registry=PendingSettlements.for_budget(self.f.b)
        self.assertEqual(len(registry.pending()),1)
        self.generate()
        self.assertEqual(self.served,[self.f.pr['id']])
        self.assertEqual(len(registry.pending()),0)
        self.assertEqual(self.holds(),[]) # Drain restores accounting, not a new paid execution.

    def test_closed_legacy_task_scopes_and_explicit_purpose_are_preserved(self):
        from dalton_core.contracts import WorkOrder
        from dalton_core.model_output_contract_hold import record
        self.generate()
        original=self.f.h.h.scheduler.work_order_authority(
            self.f.h.service.generate(**self.f.h.params,
                expected_context_hash=self.f.h.context()['content_hash'])['work_order_ref'])['work_order']
        cases=[('model-task:mission-document-extraction:0.1',None,'document_extraction'),
               ('task:document-numeric-extraction:0.1',None,'document_extraction'),
               ('task:metric-discovery-extraction:0.2',None,'document_extraction'),
               ('task:unknown-synthetic:0.1',None,None),
               ('task:document-numeric-extraction:0.1','investment_memo','investment_memo')]
        for n,(task,purpose,expected) in enumerate(cases):
            with self.subTest(task=task,purpose=purpose):
                wire=copy.deepcopy(original)
                wire['id']='work:synthetic-hold-scope-'+str(n)
                wire['idempotency_key']='synthetic-hold-scope-'+str(n)
                wire['budget']['max_output_tokens']+=n+1
                wire['budget']['max_total_tokens']+=n+1
                wire['metadata']['task_ref']=task
                if purpose is not None:wire['metadata']['purpose']=purpose
                work=WorkOrder.from_dict(wire)
                route=self.f.router.route(work,attempt_number=1,capability=work.requested_capabilities[0],
                    policy_version_ref=self.f.h.writer._document_extraction_model_config['routing_policy_ref'],
                    credential_slot_refs=[self.f.pr['credential_slot_ref'],self.backup['credential_slot_ref']],
                    required_modalities=('text',),required_context_tokens=work.budget['max_output_tokens']+10,
                    estimated_input_tokens=10,estimated_output_tokens=work.budget['max_output_tokens'],
                    purpose='document_extraction',idempotency_key='synthetic-hold-route-'+str(n))['decision']
                profile=self.f.router.get_profile(route['selected_profile_version_ref'])
                inv,result=self.respond(None,work,route,profile)
                paid=self.f.b.admit(policy_version_id='budget:owner:1',day=route['created_at'][:10],
                    work_order_ref=work.id,attempt_number=1,phase='assessment',
                    route_decision_ref=route['id'],reserved_micros=10000)
                self.f.b.settle(paid['admission_id'],actual_micros=1000)
                held=record(self.f.router,work=work,route=route,profile=profile,invocation=inv,
                    result=result,budget_store=self.f.b,admission_id=paid['admission_id'])
                self.assertEqual(held['scope']['purpose'],expected)
