"""Settled real authorities, synthetic transport; no installed credentials."""
import copy
import json
import unittest
from unittest.mock import patch

from tests import test_document_extraction_admission as fixture
from dalton_core.document_extraction import build_work
from dalton_core.extraction_successful_policy_replay import purpose_semantics
from dalton_core.research_verification import ResearchVerificationConflict
from dalton_core.store import content_hash


class SuccessfulPolicyReplayTests(unittest.TestCase):
    def setUp(self):
        self.f = fixture.BrokerAdmissionTests('test_broker_composition_reserves_then_accounts_once_and_replays')
        self.f.setUp(); self.addCleanup(self.f.doCleanups)
        self.service = self.f.h.service

    def advance(self, change=None):
        config = self.f.h.writer._document_extraction_model_config
        previous = self.f.router.get_policy(config['routing_policy_ref'])
        new = copy.deepcopy(previous); new.pop('content_hash')
        new['version'] += 1
        new['prior_version_ref'] = previous['policy_version_ref']
        new['policy_version_ref'] = previous['policy_version_ref'].rsplit(':', 1)[0]+':'+str(new['version'])
        new['thinking_level_overrides'] = {'investment_memo': {'level':'high',
            'reason':'unrelated owner selection','actor_ref':fixture.OWNER,'created_at':new['created_at']}}
        if change: change(new)
        self.f.router.register_policy(new)
        config['routing_policy_ref'] = new['policy_version_ref']
        return self.f.router.get_policy(new['policy_version_ref'])

    def paid(self):
        with self.f.patch_execute():
            result = self.f.h.generate()
        self.assertEqual(result['status'], 'succeeded', result)
        return result, self.f.h.context()

    def test_unrelated_revision_replays_complete_original_work_and_fee(self):
        first, old = self.paid()
        saved = self.f.h.h.scheduler.work_order_authority(first['work_order_ref'])
        fees = list(self.f.b.connection.execute('select * from thesis_impact_day_settlements'))
        self.advance(); self.advance()
        self.assertEqual(self.f.h.context(), old)
        with self.f.patch_execute(): second = self.f.h.generate()
        self.assertEqual(second['suggestions'], first['suggestions'])
        self.assertEqual(second['work_order_ref'], first['work_order_ref'])
        self.assertEqual(self.f.calls, 1)
        self.assertEqual(saved, self.f.h.h.scheduler.work_order_authority(first['work_order_ref']))
        self.assertEqual(fees, list(self.f.b.connection.execute('select * from thesis_impact_day_settlements')))

    def test_current_pending_authority_is_never_replaced_by_old_success(self):
        _, old = self.paid(); self.advance()
        with patch.object(self.service, '_successful_policy_context', return_value=None):
            current = self.f.h.context()
        work = build_work(current, model_config=self.f.h.writer._document_extraction_model_config)
        self.f.h.h.scheduler.enqueue(work)
        self.assertEqual(self.f.h.context(), current)
        self.assertNotEqual(current, old)
        self.assertEqual(self.f.calls, 1)

    def test_same_chain_endpoint_credential_or_profile_revision_refuses(self):
        _, old = self.paid(); self.advance()
        profile = copy.deepcopy(self.f.pr)
        profile.update(version=2, prior_version_ref=self.f.pr['profile_version_ref'],
                       profile_version_ref=self.f.pr['profile_version_ref'].rsplit(':',1)[0]+':2',
                       credential_slot_ref='credential-slot:openclaw:new-endpoint')
        self.f.router.register_profile(profile)
        self.assertNotEqual(self.f.h.context(), old)
        self.assertEqual(self.f.calls, 1)

    def test_purpose_thinking_or_filters_changes_refuse(self):
        for change in (
            lambda p:p['thinking_level_overrides'].update(document_extraction={
                'level':'high','reason':'new extraction authority','actor_ref':fixture.OWNER,'created_at':p['created_at']}),
            lambda p:p['filters'].update(allowed_providers=['different-provider']),
            lambda p:p.update(purpose_overrides={'document_extraction': {'mode':'explicit','chain':['profile:other']}})):
            with self.subTest(change=change):
                other=type(self)(); other.setUp()
                try:
                    _,old=other.paid();other.advance(change)
                    self.assertNotEqual(other.f.h.context(),old)
                    self.assertEqual(other.f.calls,1)
                finally:other.doCleanups()

    def test_unsettled_success_does_not_authorize_policy_replay(self):
        _,old=self.paid();self.advance()
        from dalton_core.thesis_impact_budget import ThesisImpactBudgetStore
        original=ThesisImpactBudgetStore.admission
        def unsettled(store,**kwargs):
            result=original(store,**kwargs)
            return {**result,'settlement':None}
        with patch.object(ThesisImpactBudgetStore,'admission',unsettled):
            self.assertNotEqual(self.f.h.context(),old)
        self.assertEqual(self.f.calls,1)

    def test_corrupt_formal_or_route_refuses_instead_of_new_paid_call(self):
        first,_=self.paid();self.advance()
        reader=self.f.h.h.scheduler.formal_result
        def corrupt(ref):
            result=reader(ref)
            return None if result is None else {**result,'content_hash':'f'*64}
        with patch.object(self.f.h.h.scheduler,'formal_result',side_effect=corrupt):
            with self.assertRaises(ResearchVerificationConflict):self.f.h.context()
        self.assertEqual(self.f.calls,1)

    def test_other_company_permission_and_original_changes_do_not_match_old_work(self):
        _,old=self.paid();new=self.advance()
        from dalton_core.extraction_successful_policy_replay import reusable_successful_context
        for field,value in (('company_ref','company:sec-cik:0000000002'),
                            ('mission_version_hash','a'*64),('source_content_hash','b'*64)):
            with self.subTest(field=field):
                base={k:v for k,v in old.items() if k not in {'id','content_hash'}}
                base[field]=value
                from dalton_core.extraction_successful_policy_replay import current_policy_context
                current=current_policy_context(base,self.f.h.writer._document_extraction_model_config,new)
                selected=reusable_successful_context(current,self.f.h.writer._document_extraction_model_config,
                    self.f.h.writer,self.f.router,self.f.b,new,purpose='document_extraction',builder=build_work)
                self.assertIsNone(selected)

    def test_each_secondary_actual_entry_replays_own_paid_success_without_prose_work(self):
        from tests.test_document_numeric_extraction import SLOTS
        for kind,field in (('numeric','figures'),('discovery','metrics')):
            with self.subTest(kind=kind):
                other=type(self)();other.setUp()
                try:
                    service=other.service;hermetic=fixture.HermeticExtractionAdapter
                    def adapter(wire,**kwargs):return hermetic({'schema_version':'0.1',field:[]},**kwargs)
                    with patch.object(fixture,'HermeticExtractionAdapter',side_effect=adapter),other.f.patch_execute(), \
                         patch.object(service,'numeric_slots',return_value=SLOTS), \
                         patch.object(service,'document_names_subject',return_value={'checked':True,'names_subject':True,'matched':[]}):
                        def invoke():
                            ctx=other.f.h.context();params={**other.f.h.params,'expected_context_hash':ctx['content_hash']}
                            return service.generate_numeric(**params,source_grade='company-filed-document') if kind=='numeric' else service.generate_metric_discovery(**params)
                        first=invoke();self.assertEqual(first['status'],'read',first)
                        other.advance();second=invoke()
                    self.assertTrue(second['replayed'],second)
                    self.assertEqual(other.f.calls,1)
                    self.assertEqual(other.f.b.connection.execute('select count(*) from thesis_impact_day_settlements').fetchone()[0],1)
                finally:other.doCleanups()

    def test_each_task_semantics_preserves_actual_worker_chain(self):
        policy=self.f.router.get_policy(fixture.policy()['policy_version_ref'])
        changed=copy.deepcopy(policy);changed.pop('content_hash')
        changed['purpose_overrides']={'document_extraction':{'mode':'explicit','chain':['profile:different']}}
        from dalton_core.model_router import _policy_wire
        changed=_policy_wire(changed)
        for purpose in ('document_extraction','document_numeric_extraction','metric_discovery_extraction'):
            self.assertNotEqual(purpose_semantics(policy,purpose),purpose_semantics(changed,purpose))

    def test_failed_historical_work_is_not_success_or_a_new_infinite_hold(self):
        self.f.actual_cost=2.0
        with self.f.patch_execute():first=self.f.h.generate()
        self.assertEqual(first['status'],'failed')
        old=self.f.h.context();fees=list(self.f.b.connection.execute('select *from thesis_impact_day_settlements'))
        self.advance();current=self.f.h.context()
        self.assertNotEqual(current,old)
        self.assertEqual(self.f.calls,1)
        self.assertEqual(fees,list(self.f.b.connection.execute('select *from thesis_impact_day_settlements')))
        self.assertEqual(self.f.h.h.scheduler.formal_result(first['work_order_ref'])['terminal_state'],'failed')

    def test_secondary_relevant_budget_change_refuses_own_success(self):
        from tests.test_document_numeric_extraction import SLOTS
        from dalton_core.document_numeric_extraction import build_work as numeric
        from dalton_core.metric_discovery_extraction import build_work as discovery
        for purpose,builder in (('document_numeric_extraction',lambda c,model_config:numeric(c,SLOTS,model_config=model_config)),
                                ('metric_discovery_extraction',discovery)):
            with self.subTest(purpose=purpose):
                old=self.f.h.context();self.advance()
                config=copy.deepcopy(self.f.h.writer._document_extraction_model_config)
                config['purpose_call_budgets']={purpose:{'max_output_tokens':17000}}
                old_work=builder(old,model_config=self.f.h.writer._document_extraction_model_config)
                self.f.h.h.scheduler.enqueue(old_work)
                selected=self.service._task_context(self.f.h.context(),config,builder)
                self.assertNotEqual(builder(selected,model_config=config).id,old_work.id)
                self.assertEqual(self.f.calls,0)


if __name__=='__main__':unittest.main()
