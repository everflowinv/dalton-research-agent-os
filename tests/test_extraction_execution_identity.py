"""Strong execution identity and real paid-window replay; synthetic transport only."""
import copy
import json
import unittest
from unittest.mock import patch
from tests import test_document_extraction_admission as fixture
from dalton_core.call_budget import freeze_shared_budget_policy
from dalton_core.document_extraction import build_work
from dalton_core.extraction_execution_identity import execution_config_binding
from dalton_core.research_verification import ResearchVerificationConflict
from dalton_core.shared_call_budget_policy import updated_shared_limits
from dalton_core.store import content_hash


class ExtractionExecutionIdentityTests(unittest.TestCase):
    def setUp(self):
        self.f=fixture.BrokerAdmissionTests('test_broker_composition_reserves_then_accounts_once_and_replays')
        self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.path=self.f.h.root/'model-policy.json';self.history=self.path.parent/'shared-call-budget-revisions';self.history.mkdir()
        body={'schema_version':'dalton-shared-call-budget-policy-0.2','default_max_cost_usd':1.0,
              'purpose_max_cost_usd':{},'purpose_call_limits':{},'purpose_run_limits':{},
              'revision':1,'prior_hash':None,'updated_at':'2026-07-10T00:00:00+00:00','actor_ref':fixture.OWNER}
        self.policy={**body,'content_hash':content_hash(body)};self.raw=copy.deepcopy(self.f.h.writer._document_extraction_model_config)
        self.raw['shared_call_budget_policy_path']=str(self.path);self.install(self.policy)

    def install(self,p):
        self.path.write_text(json.dumps(p));self.path.chmod(0o600)
        self.f.h.writer._document_extraction_model_config=freeze_shared_budget_policy(self.raw)

    def update(self,purpose='event_judgement',budget=None):
        self.history.joinpath(self.policy['content_hash']+'.json').write_text(json.dumps(self.policy))
        self.policy=updated_shared_limits(self.policy,purpose=purpose,kind='call',budget=budget or {'max_output_tokens':32768},actor_ref=fixture.OWNER,updated_at='2026-07-10T01:00:00+00:00');self.install(self.policy)

    def old_paid_window(self):
        with patch('dalton_core.extraction_execution_identity.execution_config_binding',side_effect=lambda c:{'config_hash':content_hash(c)}),self.f.patch_execute():
            result=self.f.h.generate()
            self.assertEqual(result['status'],'succeeded')
            context=self.f.h.context();saved=self.f.h.h.scheduler.work_order_authority(result['work_order_ref'])
        self.assertEqual(self.f.calls,1)
        return result,context,saved

    def test_new_work_unrelated_shared_changes_keep_identity_and_paid_replay(self):
        with self.f.patch_execute():first=self.f.h.generate()
        before=self.f.h.context();self.update();self.update('initial_screen',{'max_output_tokens':64000})
        self.assertEqual(self.f.h.context(),before)
        with self.f.patch_execute():second=self.f.h.generate()
        self.assertEqual(second['work_order_ref'],first['work_order_ref']);self.assertEqual(second['suggestions'],first['suggestions'])
        self.assertEqual(self.f.calls,1);self.assertEqual(self.f.b.connection.execute('select actual_micros from thesis_impact_day_settlements').fetchall()[0][0],1000)

    def test_old_full_work_and_fee_survive_two_unrelated_changes_without_new_call(self):
        first,old,saved=self.old_paid_window();self.update();self.update('initial_screen',{'max_output_tokens':64000})
        self.assertEqual(self.f.h.context(),old)
        with self.f.patch_execute():second=self.f.h.generate()
        self.assertEqual(second['work_order_ref'],first['work_order_ref']);self.assertEqual(self.f.calls,1)
        self.assertEqual(self.f.h.h.scheduler.work_order_authority(first['work_order_ref']),saved)
        self.assertEqual(self.f.b.connection.execute('select count(*) from thesis_impact_day_settlements').fetchone()[0],1)

    def test_real_extraction_budget_change_uses_new_identity_and_rejects_old_context(self):
        first,old,saved=self.old_paid_window();cap=saved['work_order']['budget']['max_output_tokens']
        self.update('document_extraction',{'max_output_tokens':cap+100})
        new=self.f.h.context();self.assertNotEqual(new,old)
        self.assertNotEqual(build_work(new,model_config=self.f.h.writer._document_extraction_model_config).id,first['work_order_ref'])
        with self.assertRaises(ResearchVerificationConflict):self.f.h.service.generate(**self.f.h.params, expected_context_hash=old['content_hash'])
        self.assertEqual(self.f.calls,1)

    def test_changed_model_policy_is_not_historical_replay(self):
        first,old,_=self.old_paid_window();self.update()
        p=copy.deepcopy(fixture.policy());p['policy_version_ref']='model-routing-policy-version:changed-extraction:1';p['id']='model-routing-policy:changed-extraction'
        self.f.router.register_policy(p);self.raw['routing_policy_ref']=p['policy_version_ref'];self.install(self.policy)
        self.assertNotEqual(self.f.h.context(),old)
        with self.assertRaises(ResearchVerificationConflict):self.f.h.service.generate(**self.f.h.params, expected_context_hash=old['content_hash'])
        self.assertEqual(self.f.calls,1)

    def test_missing_history_cannot_validate_old_full_context(self):
        _,old,_=self.old_paid_window();self.update()
        for p in self.history.iterdir():p.unlink()
        self.assertNotEqual(self.f.h.context(),old)
        with self.assertRaises(ResearchVerificationConflict):self.f.h.service.generate(**self.f.h.params, expected_context_hash=old['content_hash'])
        self.assertEqual(self.f.calls,1)

    def test_corrupt_policy_hash_chain_refuses_instead_of_guessing_old_bytes(self):
        self.old_paid_window();self.update()
        p=self.history.joinpath(self.policy['prior_hash']+'.json');z=json.loads(p.read_text());z['default_max_cost_usd']=99;p.write_text(json.dumps(z))
        with self.assertRaises(ResearchVerificationConflict):self.f.h.context()
        self.assertEqual(self.f.calls,1)

    def test_unshared_configuration_retains_legacy_hash_exactly(self):
        self.assertEqual(execution_config_binding(self.raw|{'shared_call_budget_policy_path':str(self.path)}).keys(),{'config_hash','config_hash_contract_ref'})
        old={k:v for k,v in self.raw.items()if k!='shared_call_budget_policy_path'}
        self.assertEqual(execution_config_binding(old),{'config_hash':content_hash(old)})

    def task_builder(self, purpose):
        if purpose == 'document_extraction':
            return build_work
        if purpose == 'document_numeric_extraction':
            from dalton_core.document_numeric_extraction import build_work as numeric
            from tests.test_document_numeric_extraction import SLOTS
            return lambda context, model_config: numeric(context, SLOTS, model_config=model_config)
        from dalton_core.metric_discovery_extraction import build_work as discovery
        return discovery

    def legacy_task(self, purpose):
        with patch('dalton_core.extraction_execution_identity.execution_config_binding', side_effect=lambda c: {'config_hash':content_hash(c)}):
            context = self.f.h.context()
        builder = self.task_builder(purpose)
        work = builder(context, model_config=self.f.h.writer._document_extraction_model_config)
        self.f.h.h.scheduler.enqueue(work)
        return context, work, builder

    def test_each_task_budget_changes_only_that_task_work(self):
        # All three real builders bind their own budget independently; changing
        # support verification or another extraction phase does not rekey them.
        config = self.f.h.writer._document_extraction_model_config
        context = self.f.h.context()
        purposes = ('document_extraction', 'document_numeric_extraction', 'metric_discovery_extraction')
        before = {p:self.task_builder(p)(context, model_config=config) for p in purposes}
        for changed in (*purposes, 'claim_support_verifier', 'claim_support_backfill'):
            with self.subTest(changed=changed):
                base = copy.deepcopy(self.policy)
                self.update(changed, {'max_output_tokens':17000})
                after_config = self.f.h.writer._document_extraction_model_config
                after_context = self.f.h.context()
                self.assertEqual(context, after_context)
                for purpose in purposes:
                    work = self.task_builder(purpose)(after_context, model_config=after_config)
                    self.assertEqual(work.id == before[purpose].id, purpose != changed)
                self.policy=base;self.install(base)

    def test_secondary_existing_work_without_qualitative_work_is_exactly_replayed(self):
        for purpose in ('document_numeric_extraction', 'metric_discovery_extraction'):
            with self.subTest(purpose=purpose):
                # Independent fixture avoids a qualitative or prior numeric
                # Work making this stage's source context look reusable.
                other=type(self)();other.setUp()
                try:
                    old, work, builder = other.legacy_task(purpose)
                    self.assertIsNone(other.f.h.h.scheduler.work_order_authority(build_work(old, model_config=other.f.h.writer._document_extraction_model_config).id))
                    saved=other.f.h.h.scheduler.work_order_authority(work.id)
                    other.update('initial_screen', {'max_output_tokens':64000})
                    service=other.f.h.service
                    current=other.f.h.context()
                    selected=service._task_context(current,other.f.h.writer._document_extraction_model_config,builder)
                    self.assertEqual(selected,old)
                    self.assertEqual(builder(selected,model_config=other.f.h.writer._document_extraction_model_config).to_dict(),work.to_dict())
                    # Exercise the actual stage entry point, not just lookup.
                    seen=[]
                    def no_send(built,ctx,*args):
                        seen.append((built.to_dict(),ctx));return None,False
                    with patch.object(service,'_run_secondary',side_effect=no_send), patch.object(service,'numeric_slots',return_value=__import__('tests.test_document_numeric_extraction',fromlist=['SLOTS']).SLOTS), patch.object(service,'document_names_subject',return_value={'checked':True,'names_subject':True,'matched':[]}):
                        params={**other.f.h.params,'expected_context_hash':current['content_hash']}
                        if purpose == 'document_numeric_extraction':
                            result=service.generate_numeric(**params,source_grade='company-filed-document')
                        else: result=service.generate_metric_discovery(**params)
                    self.assertEqual(result['status'],'no_result')
                    self.assertEqual(seen,[(work.to_dict(),old)])
                    self.assertEqual(other.f.calls,0)
                    self.assertEqual(other.f.h.h.scheduler.work_order_authority(work.id),saved)
                finally:other.doCleanups()

    def test_secondary_changed_own_budget_never_reuses_old_task_work(self):
        for purpose in ('document_numeric_extraction','metric_discovery_extraction'):
            with self.subTest(purpose=purpose):
                other=type(self)();other.setUp()
                try:
                    old,work,builder=other.legacy_task(purpose)
                    other.update(purpose,{'max_output_tokens':work.budget['max_output_tokens']+100})
                    current=other.f.h.context()
                    selected=other.f.h.service._task_context(current,other.f.h.writer._document_extraction_model_config,builder)
                    self.assertNotEqual(selected,old)
                    self.assertNotEqual(builder(selected,model_config=other.f.h.writer._document_extraction_model_config).id,work.id)
                    # A saved old budget may be read but cannot be sent under
                    # this batch's newly frozen task authority.
                    with self.assertRaises(ResearchVerificationConflict):
                        other.f.h.service._run_broker_work(work,old,fixture.OWNER)
                    self.assertEqual(other.f.calls,0)
                finally:other.doCleanups()

    def test_old_failed_paid_work_is_preserved_not_treated_as_a_new_answer(self):
        self.f.actual_cost=2.0
        with patch('dalton_core.extraction_execution_identity.execution_config_binding',side_effect=lambda c:{'config_hash':content_hash(c)}),self.f.patch_execute():
            first=self.f.h.generate()
        self.assertEqual(first['status'],'failed')
        self.assertEqual(first['error_code'],'MODEL_COST_EXCEEDED_RESERVATION')
        old=self.f.h.context();saved=self.f.h.h.scheduler.work_order_authority(first['work_order_ref'])
        self.update('event_judgement',{'max_output_tokens':32768})
        with self.f.patch_execute():second=self.f.h.generate()
        self.assertEqual(second['status'],'failed');self.assertEqual(second['work_order_ref'],first['work_order_ref'])
        self.assertEqual(self.f.calls,1)
        self.assertEqual(self.f.h.h.scheduler.work_order_authority(first['work_order_ref']),saved)
        self.assertEqual(self.f.b.connection.execute('select count(*) from thesis_impact_day_settlements').fetchone()[0],0)

    def test_unknown_old_window_stays_unknown_with_unchanged_reservation(self):
        from dalton_core.openclaw_model_adapter import BrokerConnectionError, OpenClawModelAdapter
        def unknown(adapter,work,route,profile,*,before_send=None):
            before_send();raise BrokerConnectionError('synthetic post-send unknown')
        with patch('dalton_core.extraction_execution_identity.execution_config_binding',side_effect=lambda c:{'config_hash':content_hash(c)}),patch.object(OpenClawModelAdapter,'execute',autospec=True,side_effect=unknown) as calls:
            first=self.f.h.generate()
            self.assertEqual(first['status'],'failed')
        rows=self.f.b.connection.execute('select * from thesis_impact_day_admissions').fetchall()
        formal=self.f.h.h.scheduler.formal_result(first['work_order_ref'])
        self.update('claim_support_verifier',{'max_output_tokens':17000})
        with patch.object(OpenClawModelAdapter,'execute',side_effect=AssertionError('must not repay unknown')):
            second=self.f.h.generate()
        self.assertEqual(second['status'],'failed');self.assertEqual(second['work_order_ref'],first['work_order_ref'])
        self.assertEqual(calls.call_count,1)
        self.assertEqual(self.f.h.h.scheduler.formal_result(first['work_order_ref']),formal)
        self.assertEqual(self.f.b.connection.execute('select * from thesis_impact_day_admissions').fetchall(),rows)
        self.assertEqual(self.f.b.connection.execute('select count(*) from thesis_impact_day_settlements').fetchone()[0],0)

    def test_secondary_legacy_worker_reread_and_paid_formal_replay_use_own_task(self):
        from tests import test_document_numeric_extraction as numeric_fixture
        for purpose,field in (('document_numeric_extraction','figures'),('metric_discovery_extraction','metrics')):
            with self.subTest(purpose=purpose):
                other=type(self)();other.setUp()
                try:
                    old,work,builder=other.legacy_task(purpose)
                    other.update('claim_support_backfill',{'max_output_tokens':17000})
                    service=other.f.h.service;current=other.f.h.context()
                    hermetic=fixture.HermeticExtractionAdapter
                    def task_adapter(wire,**kwargs):
                        return hermetic({'schema_version':'0.1',field:[]},**kwargs)
                    with patch.object(fixture,'HermeticExtractionAdapter',side_effect=task_adapter),other.f.patch_execute(),patch.object(service,'numeric_slots',return_value=numeric_fixture.SLOTS),patch.object(service,'document_names_subject',return_value={'checked':True,'names_subject':True,'matched':[]}):
                        params={**other.f.h.params,'expected_context_hash':current['content_hash']}
                        invoke=(lambda:service.generate_numeric(**params,source_grade='company-filed-document')) if field=='figures' else (lambda:service.generate_metric_discovery(**params))
                        first=invoke();second=invoke()
                    self.assertEqual(first['status'],'read',first)
                    self.assertEqual(first['work_order_ref'] if 'work_order_ref' in first else work.id,work.id)
                    self.assertFalse(first['replayed']);self.assertTrue(second['replayed'])
                    self.assertEqual(other.f.calls,1)
                    saved=other.f.h.h.scheduler.work_order_authority(work.id)
                    self.assertEqual(saved['work_order'],work.to_dict())
                    self.assertEqual(other.f.h.h.scheduler.formal_result(work.id)['terminal_state'],'succeeded')
                    self.assertEqual(other.f.b.connection.execute('select actual_micros from thesis_impact_day_settlements').fetchone()[0],1000)
                finally:other.doCleanups()

    def test_old_shared_schema_keeps_original_config_hash(self):
        body={'schema_version':'dalton-shared-call-budget-policy-0.1','default_max_cost_usd':1.0,'purpose_max_cost_usd':{},'revision':1,'prior_hash':None,'updated_at':'2026-07-10T00:00:00+00:00','actor_ref':fixture.OWNER}
        policy={**body,'content_hash':content_hash(body)}
        self.install(policy)
        config=self.f.h.writer._document_extraction_model_config
        self.assertEqual(execution_config_binding(config),{'config_hash':content_hash(config)})
