"""Real single-route extraction accounting drives bounded model-page history."""
import unittest
from dataclasses import replace
from unittest.mock import patch
from tests import test_document_extraction_output_hold as fixture
from dalton_core.contracts import WorkOrder
from dalton_core.model_fallback_chain import purpose_selection
from dalton_core.model_route_observation import (record_extraction_single_route,
    output_contract_hold_view, last_output_hold_skip_note)
from dalton_core.store import content_hash

class ExtractionRouteObservationTests(unittest.TestCase):
    def setUp(self):
        self.f=fixture.ExtractionOutputHoldTests('test_paid_failure_next_normal_window_uses_owner_next_profile_without_same_window_retry')
        # The fixture uses this spelling only as an identity; no TestCase is imported for discovery.
        self.f.setUp();self.addCleanup(self.f.doCleanups)
        self.f.f.h.writer._document_extraction_model_config['provider_retry'] = {
            'max_same_profile_retries': 0, 'retry_backoff_seconds': 0}

    def returned(self, offset=0):
        result=self.f.generate(offset)
        router=self.f.f.router
        route=router.list_decisions(work_order_id=result['work_order_ref'])[-1]
        work=WorkOrder.from_dict(self.f.f.h.h.scheduler.work_order_authority(result['work_order_ref'])['work_order'])
        profile=router.get_profile(route['selected_profile_version_ref'])
        invocation,envelope=self.f.respond(None,work,route,profile)
        return result,work,route,profile,invocation,envelope

    def test_actual_next_window_served_skip_and_replay_are_truthful(self):
        first=self.f.generate()
        router=self.f.f.router
        links=router.chain_links(work_order_id=first['work_order_ref'])
        self.assertEqual(len(links),1);self.assertTrue(links[0]['served'])
        self.assertEqual(links[0]['purpose'],'document_extraction')
        before=router.chain_link_digest()
        self.f.generate();self.assertEqual(router.chain_link_digest(),before)
        second=self.f.generate(fixture.fixture.WINDOW_CHARS)
        links=router.chain_links(work_order_id=second['work_order_ref'])
        self.assertEqual(sorted((x['chain_position'],x['served'],x['skip_reason']) for x in links),
                         [(1,False,'paid_output_contract_hold'),(2,True,None)])
        row=next(x for x in purpose_selection(router,last_served=router.chain_link_digest()['served_by_purpose']) if x['purpose']=='document_extraction')
        self.assertEqual(row['last_served']['profile_id'],self.f.backup['id'])
        self.assertEqual(row['last_served']['chain_position'],2)
        self.assertEqual(row['last_served']['policy_version_ref'],links[-1]['policy_version_ref'])
        self.assertIn('不是当前全局冷却',last_output_hold_skip_note(router,row['last_served']))
        self.assertEqual(router.active_cooldowns(),{})
        self.assertEqual([r[0] for r in self.f.f.b.connection.execute('SELECT actual_micros FROM thesis_impact_day_settlements')],[1000,1000])

    def test_current_hold_view_only_matches_exact_purpose_budget_policy(self):
        _,work,route,profile,inv,res=self.returned()
        router=self.f.f.router;policy=router.get_policy(route['policy_version_ref'])
        call={'max_input_tokens':work.budget['max_input_tokens'],'max_output_tokens':work.budget['max_output_tokens'],
              'max_cost_usd':work.budget['max_cost_usd'],'timeout_seconds':work.budget['max_seconds']}
        args=dict(policy=policy,purpose='document_extraction',profiles={profile['id']:profile},
                  call_budget=call,thinking_level=route['thinking_level'],now=router._now())
        view=output_contract_hold_view(router,**args)
        self.assertEqual(view[profile['id']]['scope'],'purpose_budget')
        self.assertEqual(output_contract_hold_view(router,**{**args,'purpose':'investment_memo'}),{})
        self.assertEqual(output_contract_hold_view(router,**{**args,'call_budget':{**call,'max_output_tokens':32768}}),{})
        self.assertEqual(output_contract_hold_view(router,**{**args,'policy':{**policy,'content_hash':'0'*64}}),{})

    def test_unknown_replay_and_foreign_tasks_cannot_claim_new_served_call(self):
        _,work,route,profile,inv,res=self.returned()
        router=self.f.f.router;before=router.chain_link_digest()
        def record(w=work,i=inv,r=res,**extra):return record_extraction_single_route(router,work=w,route=route,profile=profile,invocation=i,result=r,**extra)
        self.assertEqual(record(replayed=True)['status'],'historical_replay_not_backfilled')
        self.assertEqual(record(r=replace(res,metadata={**res.metadata,'dispatch_proof':{'state':'unknown'}}))['status'],'no_returned_provider_proof')
        self.assertEqual(record(i=replace(inv,usage={**inv.usage,'metering_source':'estimated'}))['status'],'no_returned_provider_proof')
        self.assertEqual(record(w=replace(work,metadata={**work.metadata,'task_ref':'task:unknown:0.1'}))['status'],'not_applicable')
        self.assertEqual(record(w=replace(work,metadata={**work.metadata,'purpose':'investment_memo'}))['status'],'not_applicable')
        self.assertEqual(router.chain_link_digest(),before)

    def test_optional_display_lock_does_not_interrupt_accounted_completion(self):
        import sqlite3
        from dalton_core import model_route_observation as observation
        with patch.object(observation,'record_extraction_single_route',side_effect=sqlite3.OperationalError('database is locked')):
            result=self.f.generate()
        self.assertEqual(result['status'],'failed')
        self.assertEqual(self.f.served,[self.f.f.pr['id']])
        self.assertEqual(self.f.f.b.connection.execute('SELECT actual_micros FROM thesis_impact_day_settlements').fetchone()[0],1000)
        self.f.generate();self.assertEqual(self.f.served,[self.f.f.pr['id']])

    def test_model_page_displays_scoped_and_historical_notes_without_edits(self):
        from tests import test_admin_model_settings_behavior as ui
        data=ui.responses();p=data['/v1/cockpit/models']['body']['purposes'][0]
        p.update(output_contract_hold_note='本环节这组上限下曾收到超额输出，暂按保存顺序使用其它模型；其它环节不受此限制。',
                 last_output_contract_skip_note='上次调用曾因本环节输出超限暂避前面的模型；这是该次调用记录，不是当前全局冷却。',
                 last_served={'display_name':'起草乙','at':'2026-01-01T00:00:00+00:00','selection_changed_since_call':True})
        runner=ui.ModelSettingsBehaviorTests('test_one_editor_and_both_independence_links_keep_checks_visible')
        out=runner.run_js("out.text=ai().textContent;out.posts=posts;",data=data)
        self.assertIn('其它环节不受此限制',out['text'])
        self.assertIn('不是当前全局冷却',out['text'])
        self.assertIn('上次调用使用的是当时保存的配置',out['text'])
        self.assertIn('起草乙',out['text']);self.assertEqual(out['posts'],[])

    def test_binding_drift_cannot_append_observation(self):
        _,work,route,profile,inv,res=self.returned()
        router=self.f.f.router;before=router.chain_link_digest()
        with self.assertRaisesRegex(ValueError,'binding differs'):
            record_extraction_single_route(router,work=work,route=route,profile=profile,
                invocation=replace(inv,work_order_ref='work:foreign-synthetic'),result=res)
        self.assertEqual(router.chain_link_digest(),before)
