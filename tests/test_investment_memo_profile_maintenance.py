"""Real Scheduler/router/day admissions, bounded fixed profile continuation."""
import copy,json,unittest
from unittest.mock import patch
from tests import test_investment_memo_terminal_maintenance as fixture
from dalton_core import investment_memo_profile_maintenance as profile
from dalton_core.cockpit_model import CockpitModelError
from dalton_core.scheduler import Scheduler
from dalton_core.investment_memo_draft import GROUPS
from tests import test_investment_memo_draft as payloads

class ProfileMaintenanceTests(unittest.TestCase):
    setUp=fixture.TerminalMaintenanceTests.setUp
    wrapper=fixture.TerminalMaintenanceTests.wrapper
    kw=fixture.TerminalMaintenanceTests.kw
    fees=fixture.TerminalMaintenanceTests.fees
    raise_policy=fixture.TerminalMaintenanceTests.raise_policy
    def seed(self,code='PROFILE_LIMIT_EXCEEDED'):
        self.raise_policy();self.adapter.script={'profile:gpt-6-astra':{'code':code,'message':'request exceeds profile limits'}}
        w=self.wrapper(112_000_000);w.begin_stage(max_units=4,allow_technical=False)
        with self.assertRaises(CockpitModelError):w.call(**self.kw())
        self.old_fees=copy.deepcopy(self.fees());self.calls=len(self.adapter.served)
        w.macro.snapshot();self.assertEqual(w.macro.last['accounted_micros'],14_000_000)
        self.assertIsNone(w.macro.extra)
        return w
    def capability(self,config,*,profile_id,model_ref):
        return {'contract_ref':'broker-profile-limits:0.1','profile_id':profile_id,'model':model_ref,
            'limits':{'maxTokens':128000,'timeoutMs':1800000},'broker_version':'test','runtime_version':'test'}
    def claims(self):
        with Scheduler(self.model.scheduler_db) as s:return s.connection.execute(f'SELECT * FROM {profile.CLAIMS}').fetchall()
    def test_fixed_once_then_success_free_replay_and_original_fees_retained(self):
        self.seed();self.adapter.script={}
        with patch.object(profile,'read_profile_limits',side_effect=self.capability):
            w=self.wrapper(112_000_000);answer=w.call(**self.kw())
            self.assertEqual(len(self.adapter.served),self.calls+1);self.assertEqual(len(self.claims()),1)
            before=self.fees();again=self.wrapper(112_000_000).call(**self.kw())
            self.assertEqual(again['text'],answer['text']);self.assertEqual(self.fees(),before)
        self.assertEqual(before[0][:len(self.old_fees[0])],self.old_fees[0]);self.assertEqual(before[1][:len(self.old_fees[1])],self.old_fees[1])
        self.assertGreaterEqual(w.macro.last['accounted_micros'],14_000_000)
    def test_missing_or_insufficient_loaded_capability_defers_no_purchase(self):
        self.seed();before=self.fees()
        for cap in (ValueError('old broker unavailable'),{'limits':{'maxTokens':1,'timeoutMs':900000}}):
            with patch.object(profile,'read_profile_limits',side_effect=cap if isinstance(cap,Exception) else None,return_value=cap):
                with self.assertRaises(CockpitModelError):self.wrapper(112_000_000).call(**self.kw())
        self.assertEqual(self.fees(),before);self.assertEqual(len(self.adapter.served),self.calls)
    def test_timeout_does_not_borrow_profile_permission(self):
        w=self.seed('TIMEOUT')
        from dalton_core.planner_paid_output_recovery import _authority_connection
        with _authority_connection(self.model.scheduler_db) as c:
            with self.assertRaisesRegex(ValueError,'pure settled'):profile.qualify(w.macro,w,c)
        self.assertEqual(len(self.adapter.served),self.calls)
    def test_new_unknown_paid_child_locked_across_budget_and_day_changes(self):
        self.seed();self.adapter.script={'profile:gpt-6-astra':{'code':'TIMEOUT','message':'host completion exceeded request timeout'}}
        with patch.object(profile,'read_profile_limits',side_effect=self.capability):
            with self.assertRaises(CockpitModelError):self.wrapper(112_000_000).call(**self.kw())
            before=self.fees();calls=len(self.adapter.served)
            self.clock.advance(86400);self.raise_policy(1800,140)
            with self.assertRaises(CockpitModelError):self.wrapper(140_000_000).call(**self.kw())
        self.assertEqual(len(self.adapter.served),calls);self.assertEqual(self.fees(),before);self.assertEqual(len(self.claims()),1)
    def test_inflight_child_still_locked_under_new_budget(self):
        self.seed();self.adapter.script={'profile:gpt-6-astra':KeyboardInterrupt('isolated crash')}
        with patch.object(profile,'read_profile_limits',side_effect=self.capability):
            # A real process crash does not run finally. KeyboardInterrupt
            # alone does: the owner then completes retryable and readies the
            # next original attempt, which is no longer an in-flight case.
            with patch('dalton_core.cockpit_model._ReleaseLeaseOnError.__exit__',return_value=False):
                with self.assertRaises(KeyboardInterrupt):self.wrapper(112_000_000).call(**self.kw())
            before=self.fees();self.raise_policy(1800,140)
            with self.assertRaises(CockpitModelError):self.wrapper(140_000_000).call(**self.kw())
        self.assertEqual(self.fees(),before)
    def test_original_current_authority_and_source_revocation_refuse(self):
        self.seed()
        with patch.object(profile,'_current',side_effect=ValueError('source or current permission revoked')):
            with self.assertRaises(CockpitModelError):self.wrapper(112_000_000).call(**self.kw())
        self.assertEqual(len(self.adapter.served),self.calls)
    def test_marker_requires_owner_authority(self):
        self.seed();before=self.fees()
        with self.assertRaisesRegex(CockpitModelError,'live owner'):
            self.model.call(**{**self.kw(),'request_id':self.kw()['request_id']+profile.MARKER+'0'*32})
        self.assertEqual(self.fees(),before)
    def test_future_budget_not_just_one_call_is_reserved(self):
        self.seed();self.raise_policy(1800,70)
        with patch.object(profile,'read_profile_limits',side_effect=self.capability):
            with self.assertRaises(CockpitModelError):self.wrapper(70_000_000).call(**self.kw())
        self.assertEqual(len(self.adapter.served),self.calls)

    def semantic_seed(self):
        self.raise_policy(900,112);self.adapter.script={};w=self.wrapper(112_000_000)
        w.call(**self.kw())
        from dalton_core.investment_memo_macro_accounting import request_id
        prompt=self.prompts[0]+'\nYour previous answer for this group was refused and was not used:\n'+"old answer and complete errors"
        kw={**self.kw(),'prompt':prompt,'request_id':request_id('identity_background',prompt)}
        # Use the real frozen group name rather than guessing an alias.
        from dalton_core.investment_memo_draft import GROUPS
        kw['request_id']=request_id(GROUPS[0][0],prompt)
        w.macro.register(role='semantic',kwargs=kw)
        w.begin_stage(max_units=1,repair=True,allow_technical=False)
        self.adapter.script={'profile:gpt-6-astra':{'code':'TIMEOUT','message':'host completion exceeded request timeout'}}
        with self.assertRaises(CockpitModelError):w.call(**kw)
        self.old_fees=copy.deepcopy(self.fees());self.calls=len(self.adapter.served)
        return prompt
    def test_registered_semantic_timeout_reuses_exact_prompt_with_one_same_macro_grant(self):
        prompt=self.semantic_seed();self.raise_policy(1800,112);self.adapter.script={}
        with patch.object(profile,'read_profile_limits',side_effect=self.capability):
            w=self.wrapper(112_000_000);answer=w.call(**self.kw())
            self.assertTrue(answer['text']);self.assertEqual(len(self.adapter.served),self.calls+1)
            with Scheduler(self.model.scheduler_db) as scheduler:
                held=scheduler.work_order_authority(answer['work_order_ref'])['work_order']
                self.assertEqual(held['question'],prompt)
            before=self.fees();self.wrapper(112_000_000).call(**self.kw());self.assertEqual(self.fees(),before)
            self.assertEqual(len(self.claims()),1)
    def test_semantic_timeout_same_deadline_not_eligible(self):
        self.semantic_seed()
        with patch.object(profile,'read_profile_limits',side_effect=self.capability):
            with self.assertRaises(CockpitModelError):self.wrapper(112_000_000).call(**self.kw())
        self.assertEqual(len(self.adapter.served),self.calls)
    def test_four_groups_and_independent_verifier_validate_then_restart_without_repurchase(self):
        self.seed();self.adapter.script={}
        from dataclasses import replace
        from types import SimpleNamespace
        from dalton_core.investment_memo_cli import run_memo
        from tests import test_cockpit_model_fallback as runtime
        execute=self.adapter.execute
        def answer(work,route,profile):
            invocation,result=execute(work,route,profile)
            if work.metadata['purpose']=='investment_memo_verifier':
                contract=json.loads(work.question.split('Return raw JSON only:\n',1)[1].split('\n',1)[0])
                value={'verdict':'pass','verified_body_hash':contract['verified_body_hash'],'finding_codes':[]}
            else:
                from dalton_core.investment_memo_timeout_recovery import prompt_identity
                index=prompt_identity(work.question)[2]
                value=payloads.group_payload(self.contexts[index]['section_titles'],self.contexts[index]['questions'])
            return invocation,replace(result,outputs={'text':json.dumps(value,ensure_ascii=False)})
        self.adapter.execute=answer
        verifier=runtime.CockpitChainTests._model(self,self.adapter,policy_version_ref=self.verifier_policy,
            slots=self.verifier_slots,clock=self.clock)
        verifier.config['budget_policy_ref']='memo-test-day-policy'
        verifier.config['purpose_call_budgets']['investment_memo_verifier']={'max_input_tokens':120000,
            'max_output_tokens':500,'max_cost_usd':14,'timeout_seconds':1800}
        frozen={'status':'ready','mission':self.mission,'playbook':{'id':'playbook:test','content_hash':'a'*64,
            'deliverable_templates':{'investment_memo':payloads.TITLES},'key_questions':payloads.QUESTIONS},
            'company':self.contexts[0]['company'],'material':self.contexts[0]['material'],
            'input_bindings':[{'ref':'claim-version:1','kind':'claim','hash':'b'*64}],'material_pack':{}}
        def run():
            w=self.wrapper(112_000_000)
            with patch('dalton_core.investment_memo_cli.MissionDeliverableAuthority') as owner,patch(
                    'dalton_core.research_product_quality.product_quality',return_value={'status':'test'}):
                owner.return_value.publish.return_value={'id':'memo:test','content_hash':'c'*64,'status':'candidate'}
                result=run_memo(store=SimpleNamespace(connection=None),frozen=frozen,model=w,
                    verifier_model=verifier,max_repairs=1,repair_budget=w)
            return result,w
        self.cap_patch=patch.object(profile,'read_profile_limits',side_effect=self.capability);self.cap_patch.start();self.addCleanup(self.cap_patch.stop)
        result,w=run();self.assertEqual(result['status'],'succeeded',result);self.assertEqual(len(self.adapter.served),self.calls+5)
        before=self.fees();result,again=run();self.assertEqual(result['status'],'succeeded',result)
        self.assertEqual(len(self.adapter.served),self.calls+5);self.assertEqual(self.fees(),before)
        self.assertGreaterEqual(again.accounted_micros,14_000_000);self.assertEqual(again.macro.extra,None)


    def test_profile_success_then_semantic_and_remaining_groups_restart(self):
        self.seed();self.adapter.script={}
        from dataclasses import replace
        from types import SimpleNamespace
        from dalton_core.investment_memo_cli import run_memo
        from tests import test_cockpit_model_fallback as runtime
        execute=self.adapter.execute
        def answer(work,route,profile):
            invocation,result=execute(work,route,profile)
            if work.metadata['purpose']=='investment_memo_verifier':
                contract=json.loads(work.question.split('Return raw JSON only:\n',1)[1].split('\n',1)[0])
                value={'verdict':'pass','verified_body_hash':contract['verified_body_hash'],'finding_codes':[]}
            else:
                from dalton_core.investment_memo_timeout_recovery import prompt_identity
                index=prompt_identity(work.question)[2]
                value=payloads.group_payload(self.contexts[index]['section_titles'],self.contexts[index]['questions'])
                if ':memo-profile-maintenance:' in work.metadata['request_id']:
                    value['key_questions']=[]
            return invocation,replace(result,outputs={'text':json.dumps(value,ensure_ascii=False)})
        self.adapter.execute=answer
        verifier=runtime.CockpitChainTests._model(self,self.adapter,policy_version_ref=self.verifier_policy,
            slots=self.verifier_slots,clock=self.clock)
        verifier.config['budget_policy_ref']='memo-test-day-policy'
        verifier.config['purpose_call_budgets']['investment_memo_verifier']={'max_input_tokens':120000,
            'max_output_tokens':500,'max_cost_usd':14,'timeout_seconds':1800}
        frozen={'status':'ready','mission':self.mission,'playbook':{'id':'playbook:test','content_hash':'a'*64,
            'deliverable_templates':{'investment_memo':payloads.TITLES},'key_questions':payloads.QUESTIONS},
            'company':self.contexts[0]['company'],'material':self.contexts[0]['material'],
            'input_bindings':[{'ref':'claim-version:1','kind':'claim','hash':'b'*64}],'material_pack':{}}
        def run():
            w=self.wrapper(112_000_000)
            with patch('dalton_core.investment_memo_cli.MissionDeliverableAuthority') as owner,patch(
                    'dalton_core.research_product_quality.product_quality',return_value={'status':'test'}):
                owner.return_value.publish.return_value={'id':'memo:test','content_hash':'c'*64,'status':'candidate'}
                result=run_memo(store=SimpleNamespace(connection=None),frozen=frozen,model=w,
                    verifier_model=verifier,max_repairs=1,repair_budget=w)
            return result,w
        self.cap_patch=patch.object(profile,'read_profile_limits',side_effect=self.capability);self.cap_patch.start();self.addCleanup(self.cap_patch.stop)
        result,w=run();self.assertEqual(result['status'],'succeeded',result);self.assertEqual(len(self.adapter.served),self.calls+6)
        before=self.fees();result,again=run();self.assertEqual(result['status'],'succeeded',result)
        self.assertEqual(len(self.adapter.served),self.calls+6);self.assertEqual(self.fees(),before)
        self.assertGreaterEqual(again.accounted_micros,14_000_000);self.assertEqual(again.macro.extra['role'],'semantic')
        self.assertEqual(len(self.claims()),1)
        with Scheduler(self.model.scheduler_db) as scheduler:
            self.assertEqual(scheduler.connection.execute(f'SELECT count(*) FROM {profile.TABLE}').fetchone()[0],1)


    def test_strong_no_send_day_refusal_defers_same_fixed_permission(self):
        self.seed();self.adapter.script={}
        from dalton_core.thesis_impact_budget import ThesisImpactBudgetStore
        with ThesisImpactBudgetStore(self.root/'budget.sqlite') as budget:
            budget.register_policy(policy_version_id='profile-low',day_cap_micros=1)
        self.model.config['budget_policy_ref']='profile-low'
        with patch.object(profile,'read_profile_limits',side_effect=self.capability):
            with self.assertRaises(CockpitModelError):self.wrapper(112_000_000).call(**self.kw())
            self.assertEqual(len(self.adapter.served),self.calls)
            with Scheduler(self.model.scheduler_db) as scheduler:
                claim=scheduler.connection.execute(f'SELECT * FROM {profile.CLAIMS}').fetchone()
                self.assertTrue(profile._no_send(scheduler.connection,self.model,claim))
            self.model.config['budget_policy_ref']='memo-test-day-policy'
            # An explicit current call-budget update creates a new no-send Work
            # under the same fixed permission; it never opens a second paid slot.
            from dalton_core.shared_call_budget_policy import updated_shared_limits
            from dalton_core.store import content_hash
            policy_path=self.root/'shared.json';policy=json.loads(policy_path.read_text())
            policy=updated_shared_limits(policy,purpose='investment_memo',kind='call',budget={'max_output_tokens':16001},actor_ref='human:owner',updated_at='2026-09-14T00:03:00+00:00')
            policy_path.write_text(json.dumps(policy))
            answer=self.wrapper(112_000_000).call(**self.kw());self.assertTrue(answer['text'])
            self.assertEqual(len(self.adapter.served),self.calls+1);self.assertEqual(len(self.claims()),2)
    def test_parent_formal_tamper_does_not_authorize(self):
        w=self.seed()
        from dalton_core.investment_memo_macro_accounting import _works
        with Scheduler(self.model.scheduler_db) as scheduler:
            work=_works(scheduler.connection,w.macro.scope['normal_requests'][0])[0][0]
            for row in scheduler.connection.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='scheduler_formal_results'").fetchall():
                scheduler.connection.execute('DROP TRIGGER '+row[0])
            scheduler.connection.execute('UPDATE scheduler_formal_results SET content_hash=? WHERE work_order_id=?',('0'*64,work.id))
        with self.assertRaises((CockpitModelError,ValueError)):self.wrapper(112_000_000).call(**self.kw())
        self.assertEqual(len(self.adapter.served),self.calls)

    def test_next_day_no_send_retries_same_fixed_grant_without_new_paid_slot(self):
        self.seed();self.adapter.script={}
        from dalton_core.thesis_impact_budget import ThesisImpactBudgetStore
        with ThesisImpactBudgetStore(self.root/'budget.sqlite') as budget:
            budget.register_policy(policy_version_id='profile-low-next-day',day_cap_micros=1)
        self.model.config['budget_policy_ref']='profile-low-next-day'
        with patch.object(profile,'read_profile_limits',side_effect=self.capability):
            with self.assertRaises(CockpitModelError):self.wrapper(112_000_000).call(**self.kw())
            self.assertEqual(len(self.adapter.served),self.calls)
            self.model.config['budget_policy_ref']='memo-test-day-policy';self.clock.advance(86400)
            answer=self.wrapper(112_000_000).call(**self.kw());self.assertTrue(answer['text'])
            self.assertEqual(len(self.adapter.served),self.calls+1);self.assertEqual(len(self.claims()),2)
            before=self.fees();self.clock.advance(86400);self.wrapper(112_000_000).call(**self.kw())
            self.assertEqual(self.fees(),before);self.assertEqual(len(self.claims()),2)


    def maintenance_then_semantic(self, outcome=None, index=0):
        self.seed();self.adapter.script={}
        with patch.object(profile,'read_profile_limits',side_effect=self.capability):
            w=self.wrapper(112_000_000);w.call(**self.kw())
        from dalton_core.investment_memo_macro_accounting import request_id
        prompt=self.prompts[index]+'\nYour previous answer for this group was refused and was not used:\ncomplete diagnostics for the existing answer'
        kwargs={**self.kw(index),'prompt':prompt,'request_id':request_id(GROUPS[index][0],prompt)}
        w.begin_stage(max_units=4,allow_technical=False,repair=True)
        self.adapter.script={} if outcome is None else {'profile:gpt-6-astra':outcome}
        return w,kwargs

    def test_later_semantic_unknown_restart_never_rebuys_or_replaces_grant(self):
        w,kwargs=self.maintenance_then_semantic({'code':'TIMEOUT','message':'host completion exceeded request timeout'})
        with self.assertRaises(CockpitModelError):w.call(**kwargs)
        fees=self.fees();calls=len(self.adapter.served);self.clock.advance(86400);self.raise_policy(1800,140)
        with patch.object(profile,'read_profile_limits',side_effect=self.capability):
            for _ in range(2):
                with self.assertRaises(CockpitModelError):self.wrapper(140_000_000).call(**self.kw())
        self.assertEqual(self.fees(),fees);self.assertEqual(len(self.adapter.served),calls);self.assertEqual(len(self.claims()),1)

    def test_later_semantic_inflight_restart_never_rebuys(self):
        w,kwargs=self.maintenance_then_semantic(KeyboardInterrupt('isolated crash after semantic admission'))
        with patch('dalton_core.cockpit_model._ReleaseLeaseOnError.__exit__',return_value=False):
            with self.assertRaises(KeyboardInterrupt):w.call(**kwargs)
        fees=self.fees();calls=len(self.adapter.served)
        with patch.object(profile,'read_profile_limits',side_effect=self.capability):
            with self.assertRaises(CockpitModelError):self.wrapper(112_000_000).call(**self.kw())
        self.assertEqual(self.fees(),fees);self.assertEqual(len(self.adapter.served),calls)

    def test_semantic_registration_without_issued_maintenance_does_not_restore_profile_parent(self):
        w=self.seed()
        from dalton_core.investment_memo_macro_accounting import request_id
        prompt=self.prompts[0]+'\nYour previous answer for this group was refused and was not used:\nforged followup'
        kw={**self.kw(),'prompt':prompt,'request_id':request_id(GROUPS[0][0],prompt)}
        w.macro.register(role='semantic',kwargs=kw)
        with Scheduler(self.model.scheduler_db) as scheduler:
            with self.assertRaises(ValueError):profile.qualify(w.macro,w,scheduler.connection)
        self.assertEqual(len(self.adapter.served),self.calls)

    def test_semantic_registration_after_unsuccessful_maintenance_remains_unproved(self):
        self.seed();self.adapter.script={'profile:gpt-6-astra':{'code':'TIMEOUT','message':'host completion exceeded request timeout'}}
        with patch.object(profile,'read_profile_limits',side_effect=self.capability):
            w=self.wrapper(112_000_000)
            with self.assertRaises(CockpitModelError):w.call(**self.kw())
        from dalton_core.investment_memo_macro_accounting import request_id
        prompt=self.prompts[0]+'\nYour previous answer for this group was refused and was not used:\nforged followup'
        kw={**self.kw(),'prompt':prompt,'request_id':request_id(GROUPS[0][0],prompt)}
        w.macro.register(role='semantic',kwargs=kw);fees=self.fees();calls=len(self.adapter.served)
        with Scheduler(self.model.scheduler_db) as scheduler:
            with self.assertRaisesRegex(ValueError,'maintenance success'):profile.qualify(w.macro,w,scheduler.connection)
        self.assertEqual(self.fees(),fees);self.assertEqual(len(self.adapter.served),calls)

    def test_later_semantic_registration_group_or_hash_tamper_refuses(self):
        w,kwargs=self.maintenance_then_semantic();w.call(**kwargs)
        from dalton_core.investment_memo_macro_accounting import REQUESTS,_body
        from dalton_core.store import canonical_json,content_hash
        fees=self.fees();calls=len(self.adapter.served)
        with Scheduler(self.model.scheduler_db) as scheduler:
            for row in scheduler.connection.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name=?",(REQUESTS,)).fetchall():
                scheduler.connection.execute('DROP TRIGGER '+row[0])
            row=scheduler.connection.execute(f'SELECT * FROM {REQUESTS} WHERE request_key=?',(w.macro.ref+':'+kwargs['request_id'],)).fetchone()
            original=_body(row)
            for field,value in (('group_index',1),('prompt_hash','0'*64),('purpose','investment_memo_verifier')):
                body={**original,field:value}
                scheduler.connection.execute(f'UPDATE {REQUESTS} SET record_json=?,content_hash=? WHERE request_key=?',
                    (canonical_json(body),content_hash(body),row['request_key']))
                with self.assertRaises(ValueError):profile.qualify(w.macro,w,scheduler.connection)
        self.assertEqual(self.fees(),fees);self.assertEqual(len(self.adapter.served),calls)

    def test_maintenance_success_formal_tamper_after_semantic_refuses(self):
        w,kwargs=self.maintenance_then_semantic();w.call(**kwargs)
        fees=self.fees();calls=len(self.adapter.served)
        with Scheduler(self.model.scheduler_db) as scheduler:
            claim=self.claims()[0]
            for row in scheduler.connection.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='scheduler_formal_results'").fetchall():
                scheduler.connection.execute('DROP TRIGGER '+row[0])
            scheduler.connection.execute('UPDATE scheduler_formal_results SET content_hash=? WHERE work_order_id=?',('0'*64,claim['work_ref']))
            with self.assertRaises(ValueError):profile.qualify(w.macro,w,scheduler.connection)
        self.assertEqual(self.fees(),fees);self.assertEqual(len(self.adapter.served),calls)

    def test_valid_later_other_group_semantic_keeps_original_profile_proof(self):
        w,kwargs=self.maintenance_then_semantic(index=1)
        with Scheduler(self.model.scheduler_db) as scheduler:before=profile.persisted_entry(w.macro,scheduler.connection)[1]
        w.call(**kwargs)
        with Scheduler(self.model.scheduler_db) as scheduler:after=profile.persisted_entry(w.macro,scheduler.connection)[1]
        self.assertEqual(before,after);self.assertEqual(len(self.claims()),1)
        fees=self.fees();calls=len(self.adapter.served)
        with patch.object(profile,'read_profile_limits',side_effect=self.capability):
            self.wrapper(112_000_000).call(**self.kw(1))
        self.assertEqual(self.fees(),fees);self.assertEqual(len(self.adapter.served),calls)


    def test_original_semantic_timeout_maintenance_four_groups_restart_free(self):
        self.semantic_seed();self.raise_policy(1800,112);self.adapter.script={}
        from dataclasses import replace
        from types import SimpleNamespace
        from dalton_core.investment_memo_cli import run_memo
        from tests import test_cockpit_model_fallback as runtime
        execute=self.adapter.execute
        def answer(work,route,profile):
            invocation,result=execute(work,route,profile)
            if work.metadata['purpose']=='investment_memo_verifier':
                contract=json.loads(work.question.split('Return raw JSON only:\n',1)[1].split('\n',1)[0])
                value={'verdict':'pass','verified_body_hash':contract['verified_body_hash'],'finding_codes':[]}
            else:
                from dalton_core.investment_memo_timeout_recovery import prompt_identity
                index=prompt_identity(work.question)[2]
                value=payloads.group_payload(self.contexts[index]['section_titles'],self.contexts[index]['questions'])
            return invocation,replace(result,outputs={'text':json.dumps(value,ensure_ascii=False)})
        self.adapter.execute=answer
        verifier=runtime.CockpitChainTests._model(self,self.adapter,policy_version_ref=self.verifier_policy,
            slots=self.verifier_slots,clock=self.clock)
        verifier.config['budget_policy_ref']='memo-test-day-policy'
        verifier.config['purpose_call_budgets']['investment_memo_verifier']={'max_input_tokens':120000,
            'max_output_tokens':500,'max_cost_usd':14,'timeout_seconds':1800}
        frozen={'status':'ready','mission':self.mission,'playbook':{'id':'playbook:test','content_hash':'a'*64,
            'deliverable_templates':{'investment_memo':payloads.TITLES},'key_questions':payloads.QUESTIONS},
            'company':self.contexts[0]['company'],'material':self.contexts[0]['material'],
            'input_bindings':[{'ref':'claim-version:1','kind':'claim','hash':'b'*64}],'material_pack':{}}
        def run():
            w=self.wrapper(112_000_000)
            with patch('dalton_core.investment_memo_cli.MissionDeliverableAuthority') as owner,patch(
                    'dalton_core.research_product_quality.product_quality',return_value={'status':'test'}):
                owner.return_value.publish.return_value={'id':'memo:test','content_hash':'c'*64,'status':'candidate'}
                result=run_memo(store=SimpleNamespace(connection=None),frozen=frozen,model=w,
                    verifier_model=verifier,max_repairs=1,repair_budget=w)
            return result,w
        self.cap_patch=patch.object(profile,'read_profile_limits',side_effect=self.capability);self.cap_patch.start();self.addCleanup(self.cap_patch.stop)
        result,w=run();self.assertEqual(result['status'],'succeeded',result);self.assertEqual(len(self.adapter.served),self.calls+5)
        before=self.fees();result,again=run();self.assertEqual(result['status'],'succeeded',result)
        self.assertEqual(len(self.adapter.served),self.calls+5);self.assertEqual(self.fees(),before)
        self.assertGreaterEqual(again.accounted_micros,14_000_000);self.assertEqual(again.macro.extra['role'],'semantic')
        self.assertEqual(len(self.claims()),1)


    def test_later_normal_timeout_preserves_original_profile_grant_and_fees(self):
        self.seed();self.adapter.script={}
        with patch.object(profile,'read_profile_limits',side_effect=self.capability):
            w=self.wrapper(112_000_000);w.call(**self.kw())
        with Scheduler(self.model.scheduler_db) as scheduler:before=profile.persisted_entry(w.macro,scheduler.connection)[1]
        self.adapter.script={'profile:gpt-6-astra':{'code':'TIMEOUT','message':'host completion exceeded request timeout'}}
        w.begin_stage(max_units=4,allow_technical=False)
        with self.assertRaises(CockpitModelError):w.call(**self.kw(1))
        fees=self.fees();calls=len(self.adapter.served)
        with Scheduler(self.model.scheduler_db) as scheduler:after=profile.persisted_entry(w.macro,scheduler.connection)[1]
        self.assertEqual(before,after)
        with patch.object(profile,'read_profile_limits',side_effect=self.capability):
            self.wrapper(112_000_000).call(**self.kw())
            retry=self.wrapper(112_000_000);retry.begin_stage(max_units=4,allow_technical=False)
            with self.assertRaises(CockpitModelError):retry.call(**self.kw(1))
        self.assertEqual(self.fees(),fees);self.assertEqual(len(self.adapter.served),calls);self.assertEqual(len(self.claims()),1)

    def test_later_normal_timeout_after_semantic_keeps_original_parent(self):
        w,kwargs=self.maintenance_then_semantic();w.call(**kwargs)
        with Scheduler(self.model.scheduler_db) as scheduler:before=profile.persisted_entry(w.macro,scheduler.connection)[1]
        self.adapter.script={'profile:gpt-6-astra':{'code':'TIMEOUT','message':'host completion exceeded request timeout'}}
        w.begin_stage(max_units=4,allow_technical=False)
        with self.assertRaises(CockpitModelError):w.call(**self.kw(1))
        fees=self.fees();calls=len(self.adapter.served)
        with Scheduler(self.model.scheduler_db) as scheduler:after=profile.persisted_entry(w.macro,scheduler.connection)[1]
        self.assertEqual(before,after)
        with patch.object(profile,'read_profile_limits',side_effect=self.capability):self.wrapper(112_000_000).call(**self.kw())
        self.assertEqual(self.fees(),fees);self.assertEqual(len(self.adapter.served),calls);self.assertEqual(len(self.claims()),1)


    def test_later_normal_profile_refusal_cannot_borrow_original_fixed_permission(self):
        self.seed();self.adapter.script={}
        with patch.object(profile,'read_profile_limits',side_effect=self.capability):
            w=self.wrapper(112_000_000);w.call(**self.kw())
        with Scheduler(self.model.scheduler_db) as scheduler:before=profile.persisted_entry(w.macro,scheduler.connection)[1]
        self.adapter.script={'profile:gpt-6-astra':{'code':'PROFILE_LIMIT_EXCEEDED','message':'request exceeds profile limits'}}
        with patch.object(profile,'read_profile_limits',side_effect=self.capability):
            with self.assertRaises(CockpitModelError):w.call(**self.kw(1))
        fees=self.fees();calls=len(self.adapter.served)
        with Scheduler(self.model.scheduler_db) as scheduler:after=profile.persisted_entry(w.macro,scheduler.connection)[1]
        self.assertEqual(before,after)
        with patch.object(profile,'read_profile_limits',side_effect=self.capability):
            for _ in range(2):
                with self.assertRaises(CockpitModelError):self.wrapper(112_000_000).call(**self.kw(1))
        self.assertEqual(self.fees(),fees);self.assertEqual(len(self.adapter.served),calls);self.assertEqual(len(self.claims()),1)

    def test_existing_later_technical_slot_keeps_original_profile_proof(self):
        self.seed();self.adapter.script={}
        with patch.object(profile,'read_profile_limits',side_effect=self.capability):
            w=self.wrapper(112_000_000);w.call(**self.kw())
        with Scheduler(self.model.scheduler_db) as scheduler:before=profile.persisted_entry(w.macro,scheduler.connection)[1]
        self.adapter.script={'profile:gpt-6-astra':{'code':'TIMEOUT','message':'host completion exceeded request timeout'}}
        w.begin_stage(max_units=4,allow_technical=False)
        with self.assertRaises(CockpitModelError):w.call(**self.kw(1))
        from dalton_core.investment_memo_macro_accounting import _works
        with Scheduler(self.model.scheduler_db) as scheduler:
            work=_works(scheduler.connection,w.macro.scope['normal_requests'][1])[-1][0]
        # Claim only the original existing technical slot using its actual
        # terminal proof. This fix never authorizes a second capability call.
        w.macro.claim_technical(work.id)
        fees=self.fees();calls=len(self.adapter.served)
        with Scheduler(self.model.scheduler_db) as scheduler:after=profile.persisted_entry(w.macro,scheduler.connection)[1]
        self.assertEqual(before,after)
        with patch.object(profile,'read_profile_limits',side_effect=self.capability):self.wrapper(112_000_000).call(**self.kw())
        self.assertEqual(self.fees(),fees);self.assertEqual(len(self.adapter.served),calls);self.assertEqual(len(self.claims()),1)


    def test_semantic_timeout_issued_grant_survives_later_normal_failure(self):
        self.semantic_seed();self.raise_policy(1800,112);self.adapter.script={}
        with patch.object(profile,'read_profile_limits',side_effect=self.capability):
            w=self.wrapper(112_000_000);w.call(**self.kw())
        with Scheduler(self.model.scheduler_db) as scheduler:before=profile.persisted_entry(w.macro,scheduler.connection)[1]
        self.adapter.script={'profile:gpt-6-astra':{'code':'TIMEOUT','message':'host completion exceeded request timeout'}}
        w.begin_stage(max_units=4,allow_technical=False)
        with self.assertRaises(CockpitModelError):w.call(**self.kw(1))
        fees=self.fees();calls=len(self.adapter.served)
        with Scheduler(self.model.scheduler_db) as scheduler:after=profile.persisted_entry(w.macro,scheduler.connection)[1]
        self.assertEqual(before,after)
        with patch.object(profile,'read_profile_limits',side_effect=self.capability):
            result=self.wrapper(112_000_000).call(**self.kw());self.assertTrue(result['text'])
        self.assertEqual(self.fees(),fees);self.assertEqual(len(self.adapter.served),calls);self.assertEqual(len(self.claims()),1)
