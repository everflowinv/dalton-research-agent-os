"""Real current mission + original Scheduler/router/fees; no live providers."""
import copy,json,unittest
from unittest.mock import patch
from dalton_core import investment_memo_terminal_maintenance as terminal
from dalton_core.cockpit_model import CockpitModelError
from dalton_core.investment_memo_repair_budget import MemoRepairBudget
from dalton_core.investment_memo_draft import GROUPS,build_group_prompt,draft_group
from dalton_core.investment_memo_macro_accounting import request_id,EXTRA,_body
from dalton_core.scheduler import Scheduler
from dalton_core.store import DaltonStore,content_hash
from dalton_core.coverage_mission import CoverageMissionAuthority
from dalton_core.shared_call_budget_policy import updated_shared_limits,SCHEMA_VERSION
from tests import test_investment_memo_timeout_recovery as transport
from tests import test_investment_memo_draft as payloads
from tests.p9a_fixtures import bootstrap_method_authorities,mission_params


class TerminalMaintenanceTests(unittest.TestCase):
    def setUp(self):
        transport.MemoTimeoutRecoveryTests.setUp(self)
        self.store=DaltonStore(str(self.root/'core.sqlite'));self.addCleanup(self.store.close)
        methods=bootstrap_method_authorities(self.store);self.missions=CoverageMissionAuthority(self.store)
        params=mission_params(methods)
        params['autonomy']['may_write'].extend(['deliverable','source_discovery'])
        params['budget']['max_daily_cost_usd']=1000;params['budget']['max_daily_paid_calls']=100
        created=self.missions.create_mission(params.pop('mission_ref'),**params)
        self.mission=self.missions._mission_record(created['id'])
        self.company=self.mission['universe'][0]['company_ref']
        questions=[{'question_ref':f'memo_q{i:02d}','question':q} for i,q in enumerate(payloads.QUESTIONS,1)]
        self.contexts=[dict(group=group,section_titles=[payloads.TITLES[i] for i in si],
            questions=[questions[i] for i in qi],material=[{'ref':'claim-version:1','kind':'claim','text':'已核对的研究。'}],
            company={'company_ref':self.company}) for group,si,qi in GROUPS]
        self.prompts=[build_group_prompt(**c) for c in self.contexts]
        self.model.config['purpose_call_budgets']['investment_memo']['max_cost_usd']=14

    def wrapper(self,run=84_000_000):
        w=MemoRepairBudget(self.model,run_micros=run,producer_micros=14_000_000,verifier_micros=14_000_000)
        w.bind_macro(self.prompts,self.mission,allow_maintenance=True);w.begin_stage(max_units=4,allow_technical=True);return w
    def kw(self,index=0):return {'purpose':'investment_memo','request_id':request_id(GROUPS[index][0],self.prompts[index]),'prompt':self.prompts[index],'mission':self.mission}
    def fees(self):return transport.MemoTimeoutRecoveryTests.fee_rows(self)
    def seed(self):
        self.adapter.script={'profile:gpt-6-astra':{'code':'TIMEOUT','message':'host completion exceeded request timeout'}}
        w=self.wrapper();w.begin_stage(max_units=4,allow_technical=False)
        with self.assertRaises(CockpitModelError):w.call(**self.kw())
        self.clock.advance(61);w=self.wrapper()
        with self.assertRaises(CockpitModelError):w.call(**self.kw())
        self.assertEqual(len(self.adapter.served),2)
        w.macro.snapshot()
        self.old_fees=self.fees();self.old_extra=copy.deepcopy(w.macro.extra)
        self.assertEqual(self.old_extra['role'],'technical');self.assertEqual(w.macro.last['accounted_micros'],28_000_000)
        return w
    def raise_policy(self,timeout=1800,run=112):
        body={'schema_version':SCHEMA_VERSION,'default_max_cost_usd':14,'purpose_max_cost_usd':{},'revision':1,
            'prior_hash':None,'updated_at':'2026-09-14T00:00:00+00:00','actor_ref':'human:owner'}
        p={**body,'content_hash':content_hash(body)}
        p=updated_shared_limits(p,purpose='investment_memo',kind='call',budget={'timeout_seconds':timeout},actor_ref='human:owner',updated_at='2026-09-14T00:01:00+00:00')
        p=updated_shared_limits(p,purpose='investment_memo',kind='run',budget={'max_cost_usd':run,'max_units':4},actor_ref='human:owner',updated_at='2026-09-14T00:02:00+00:00')
        path=self.root/'shared.json';path.write_text(json.dumps(p));self.model.config['shared_call_budget_policy_path']=str(path)
    def claim(self):
        with Scheduler(self.model.scheduler_db) as s:return s.connection.execute(f'SELECT * FROM {terminal.CLAIMS}').fetchall()

    def test_shared_increase_one_new_work_and_restart_free_replay_preserve_old_fees(self):
        self.seed();self.raise_policy();self.adapter.script={};w=self.wrapper(112_000_000)
        call=w.call(**self.kw());self.assertEqual(len(self.adapter.served),3)
        self.assertEqual(len(self.claim()),1);new=self.fees()
        self.assertEqual(new[0][:len(self.old_fees[0])],self.old_fees[0]);self.assertEqual(new[1][:len(self.old_fees[1])],self.old_fees[1])
        self.assertEqual(w.macro.extra,self.old_extra)
        self.assertEqual(self.wrapper(112_000_000).call(**self.kw())['text'],call['text']);self.assertEqual(len(self.adapter.served),3)
        self.assertGreaterEqual(w.macro.last['accounted_micros'],28_000_000)

    def test_same_or_only_one_budget_field_increase_does_not_send(self):
        self.seed();before=self.fees()
        for timeout,run in ((900,84),(900,112),(1800,84),(1800,90)):
            self.raise_policy(timeout,run)
            with self.assertRaises(CockpitModelError):self.wrapper(int(run*1_000_000)).call(**self.kw())
        self.assertEqual(len(self.adapter.served),2);self.assertEqual(self.fees(),before)

    def test_unknown_new_child_is_locked_across_day_restart_and_further_budget_increase(self):
        self.seed();self.raise_policy()
        with self.assertRaises(CockpitModelError):self.wrapper(112_000_000).call(**self.kw())
        self.assertEqual(len(self.adapter.served),3);before=self.fees();self.clock.advance(86400)
        self.raise_policy(3600,140)
        for _ in range(2):
            with self.assertRaises(CockpitModelError):self.wrapper(140_000_000).call(**self.kw())
        self.assertEqual(len(self.adapter.served),3);self.assertEqual(self.fees(),before);self.assertEqual(len(self.claim()),1)

    def test_inflight_new_child_is_locked_under_changed_policy(self):
        self.seed();self.raise_policy()
        self.adapter.script={'profile:gpt-6-astra':KeyboardInterrupt('isolated crash')}
        with self.assertRaises(KeyboardInterrupt):self.wrapper(112_000_000).call(**self.kw())
        self.raise_policy(3600,140);before=len(self.adapter.served)
        with self.assertRaises(CockpitModelError):self.wrapper(140_000_000).call(**self.kw())
        self.assertEqual(len(self.adapter.served),before)

    def test_missing_source_or_current_permission_does_not_buy(self):
        self.seed();self.raise_policy();self.adapter.script={}
        with patch.object(terminal,'_current',side_effect=ValueError('acquired original absent')):
            with self.assertRaises(CockpitModelError):self.wrapper(112_000_000).call(**self.kw())
        with patch.object(terminal,'_current',side_effect=ValueError('current authority revoked')):
            with self.assertRaises(CockpitModelError):self.wrapper(112_000_000).call(**self.kw())
        self.assertEqual(len(self.adapter.served),2)

    def test_new_marker_without_live_authority_refuses_zero_calls(self):
        self.seed();self.raise_policy();before=self.fees()
        with self.assertRaisesRegex(CockpitModelError,'live owner'):
            self.model.call(**{**self.kw(),'request_id':self.kw()['request_id']+terminal.MARKER+'0'*32})
        self.assertEqual(len(self.adapter.served),2);self.assertEqual(self.fees(),before)

    def test_old_unknown_fee_settlement_missing_does_not_authorize(self):
        self.seed();self.raise_policy();before=len(self.adapter.served)
        with patch('dalton_core.investment_memo_terminal_maintenance.qualify',side_effect=ValueError('original settlement incomplete')):
            with self.assertRaises(CockpitModelError):self.wrapper(112_000_000).call(**self.kw())
        self.assertEqual(len(self.adapter.served),before)

    def test_pause_before_new_call_does_not_consume_permission(self):
        self.seed();self.raise_policy();self.adapter.script={};w=self.wrapper(112_000_000)
        checkpoint=unittest.mock.Mock();checkpoint.checkpoint.side_effect=RuntimeError('cooperative pause')
        w.begin_stage(max_units=4,allow_technical=True,quiesce=checkpoint)
        with self.assertRaisesRegex(RuntimeError,'cooperative pause'):w.call(**self.kw())
        self.assertEqual(len(self.adapter.served),2)

    def test_cross_company_scope_and_changed_source_prompt_refuse(self):
        self.seed();self.raise_policy();w=self.wrapper(112_000_000)
        from dalton_core.planner_paid_output_recovery import _authority_connection
        with _authority_connection(self.model.scheduler_db) as c:
            w.macro.scope={**w.macro.scope,'company_ref':'company:foreign'}
            with self.assertRaisesRegex(ValueError,'scope differs'):terminal.qualify(w.macro,w,c)
        self.assertEqual(len(self.adapter.served),2)

    def test_real_current_owner_revocation_refuses_without_borrowing_paid_old_mission(self):
        self.seed();self.raise_policy();params=mission_params(bootstrap_method_authorities(self.store))
        params['autonomy']['may_write'].append('source_discovery')
        params['budget']['max_daily_cost_usd']=1000;params['budget']['max_daily_paid_calls']=100
        params.update(version_id=self.mission['id']+':revoked',prior_version_ref=self.mission['id'],idempotency_key='terminal-test-revoke')
        created=self.missions.create_mission(params.pop('mission_ref'),**params)
        self.mission=self.missions._mission_record(created['id'])
        self.assertNotIn('deliverable',self.mission['autonomy']['may_write'])
        from dalton_core.planner_paid_output_recovery import _authority_connection
        wrapper=self.wrapper(112_000_000)
        with _authority_connection(self.model.scheduler_db) as connection:
            with self.assertRaisesRegex(ValueError,'current permission'):
                terminal.qualify(wrapper.macro,wrapper,connection)
        self.assertEqual(len(self.adapter.served),2)

    def test_original_loader_optional_source_metadata_preserves_default_wire(self):
        from tests import test_evidence_index as corpus
        from dalton_core import evidence_index,initial_screen_failure_feedback as feedback
        state=self.root/'originals';state.mkdir();builder=corpus.CorpusBuilder(state);self.addCleanup(builder.close)
        doc=builder.alphaengine('one','100001',corpus.TRANSCRIPT,spec='earnings-call-transcripts',
            title='Synthetic source',broker=None,published='2026-10-01',named=['Synthetic Widgets'])
        builder.db.commit();evidence_index.index_acquired_manifest(state,state/'acquisitions/one/manifest.json')
        refs={evidence_index.passages_for_document(state,doc)[0]['passage_ref']}
        old=feedback._checked_originals(state,refs)
        new=feedback._checked_originals(state,refs,include_source_metadata=True)
        self.assertEqual({ref:{k:v for k,v in body.items() if k!='source_ref'} for ref,body in new.items()},old)
        self.assertEqual({body['source_ref'] for body in new.values()},{'source:alphaengine'})

    def test_guard_policy_drift_before_admission_refuses_zero_new_calls(self):
        self.seed();self.raise_policy();w=self.wrapper(112_000_000)
        from dalton_core.planner_paid_output_recovery import _authority_connection
        with _authority_connection(self.model.scheduler_db) as c:
            proof=terminal.qualify(w.macro,w,c);policy=terminal._policy(w.macro,w,proof['original_limits'])
        authority=terminal.Authority(w.macro,w,proof,policy)
        self.raise_policy(3600,140)
        self.model._memo_terminal_maintenance_authority=authority
        request=w.macro.scope['normal_requests'][0]['request_id']+terminal.MARKER+content_hash(proof)[:32]
        try:
            with self.assertRaisesRegex(CockpitModelError,'authority differs'):
                self.model.call(**{**self.kw(),'request_id':request})
        finally:del self.model._memo_terminal_maintenance_authority
        self.assertEqual(len(self.adapter.served),2)

    def test_failure_reporter_wrapper_carries_live_authority_to_actual_transport(self):
        self.seed();self.raise_policy();self.adapter.script={}
        from dalton_core.investment_memo_failure_cost import MemoFailureCostReporter
        report=MemoFailureCostReporter(self.model);w=MemoRepairBudget(report,run_micros=112_000_000,
            producer_micros=14_000_000,verifier_micros=14_000_000)
        w.bind_macro(self.prompts,self.mission);w.begin_stage(max_units=4,allow_technical=True)
        w.call(**self.kw());self.assertEqual(len(self.adapter.served),3);self.assertEqual(len(self.claim()),1)

    def test_four_groups_and_independent_verifier_validate_then_restart_without_repurchase(self):
        self.seed();self.raise_policy();self.adapter.script={}
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
        result,w=run();self.assertEqual(result['status'],'succeeded',result);self.assertEqual(len(self.adapter.served),7)
        before=self.fees();result,again=run();self.assertEqual(result['status'],'succeeded',result)
        self.assertEqual(len(self.adapter.served),7);self.assertEqual(self.fees(),before)
        self.assertGreaterEqual(again.accounted_micros,28_000_000);self.assertEqual(again.macro.extra,self.old_extra)

    def test_two_issuer_refs_share_generic_rule_with_distinct_fixed_macros(self):
        refs=[]
        for member in (0,1):
            fixture=TerminalMaintenanceTests();fixture.setUp();self.addCleanup(fixture.doCleanups)
            company=fixture.mission['universe'][member]['company_ref']
            fixture.contexts=[{**c,'company':{'company_ref':company}} for c in fixture.contexts]
            fixture.prompts=[build_group_prompt(**c) for c in fixture.contexts]
            fixture.seed();fixture.raise_policy();fixture.adapter.script={}
            w=fixture.wrapper(112_000_000);w.call(**fixture.kw());refs.append(w.macro.ref)
            self.assertEqual(len(fixture.adapter.served),3);self.assertEqual(len(fixture.claim()),1)
        self.assertNotEqual(*refs)

    def test_actual_unsettled_original_successor_keeps_reservation_and_cannot_send_again(self):
        self.adapter.script={'profile:gpt-6-astra':{'code':'TIMEOUT','message':'host completion exceeded request timeout'}}
        w=self.wrapper();w.begin_stage(max_units=4,allow_technical=False)
        with self.assertRaises(CockpitModelError):w.call(**self.kw())
        self.clock.advance(61);w=self.wrapper()
        with patch('dalton_core.cockpit_model._settle_without_losing_the_lease',return_value=None):
            with self.assertRaises(CockpitModelError):w.call(**self.kw())
        before=len(self.adapter.served);self.raise_policy();self.adapter.script={}
        with self.assertRaises(CockpitModelError):self.wrapper(112_000_000).call(**self.kw())
        self.assertEqual(len(self.adapter.served),before)

    def test_original_non_timeout_successor_failure_is_not_eligible(self):
        self.adapter.script={'profile:gpt-6-astra':{'code':'TIMEOUT','message':'host completion exceeded request timeout'}}
        w=self.wrapper();w.begin_stage(max_units=4,allow_technical=False)
        with self.assertRaises(CockpitModelError):w.call(**self.kw())
        self.clock.advance(61);self.adapter.script={'profile:gpt-6-astra':{'code':'PROVIDER_ERROR','message':'unknown actual error'}}
        w=self.wrapper()
        with self.assertRaises(CockpitModelError):w.call(**self.kw())
        self.raise_policy();before=len(self.adapter.served)
        with self.assertRaises(CockpitModelError):self.wrapper(112_000_000).call(**self.kw())
        self.assertEqual(len(self.adapter.served),before)

    def test_original_formal_hash_tamper_does_not_authorize_new_work(self):
        self.seed();self.raise_policy();before=len(self.adapter.served)
        with Scheduler(self.model.scheduler_db) as scheduler:
            parent=self.old_extra['parent_work_ref']
            for row in scheduler.connection.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='scheduler_formal_results'").fetchall():
                scheduler.connection.execute('DROP TRIGGER '+row[0])
            scheduler.connection.execute('UPDATE scheduler_formal_results SET content_hash=? WHERE work_order_id=?',('0'*64,parent))
        with self.assertRaises((CockpitModelError,ValueError)):self.wrapper(112_000_000).call(**self.kw())
        self.assertEqual(len(self.adapter.served),before)

    def test_profile_limit_failure_is_not_a_terminal_timeout_permission(self):
        self.adapter.script={'profile:gpt-6-astra':{'code':'TIMEOUT','message':'host completion exceeded request timeout'}}
        w=self.wrapper();w.begin_stage(max_units=4,allow_technical=False)
        with self.assertRaises(CockpitModelError):w.call(**self.kw())
        self.clock.advance(61)
        self.adapter.script={'profile:gpt-6-astra':{'code':'PROFILE_LIMIT_EXCEEDED','message':'profile timeout ceiling'}}
        with self.assertRaises(CockpitModelError):self.wrapper().call(**self.kw())
        self.raise_policy();before=len(self.adapter.served)
        with self.assertRaises(CockpitModelError):self.wrapper(112_000_000).call(**self.kw())
        self.assertEqual(len(self.adapter.served),before)

    def test_original_scope_default_remains_strict_after_owned_new_fee(self):
        self.seed();self.raise_policy();self.adapter.script={}
        w=self.wrapper(112_000_000);w.call(**self.kw())
        from dalton_core.cockpit_timeout_recovery import verify
        from dalton_core.planner_paid_output_recovery import recovery_readers
        from dalton_core.investment_memo_macro_accounting import _works
        with Scheduler(self.model.scheduler_db) as scheduler,recovery_readers(
                self.model.config['model_router_db'],self.model.config['budget_db'],now=self.clock()) as (router,budget):
            child=next(work for work,_ in _works(scheduler.connection,w.macro.scope['normal_requests'][0])
                       if work.metadata.get('timeout_recovery'))
            with self.assertRaisesRegex(ValueError,'historical macro bills differ'):
                verify(scheduler.connection,router,budget,child.metadata['timeout_recovery'],mission=self.mission)
            proof=terminal.qualify(w.macro,w,scheduler.connection)
            self.assertEqual(proof['parent']['accounted_micros']+proof['used_successor']['accounted_micros'],28_000_000)
        w.macro.snapshot();self.assertGreater(w.macro.last['accounted_micros'],28_000_000)

    def test_strong_local_budget_refusal_can_defer_unused_permission_only(self):
        self.seed();self.raise_policy();self.adapter.script={}
        from dalton_core.thesis_impact_budget import ThesisImpactBudgetStore
        with ThesisImpactBudgetStore(self.root/'budget.sqlite') as budget:
            budget.register_policy(policy_version_id='terminal-low',day_cap_micros=1)
        self.model.config['budget_policy_ref']='terminal-low'
        with self.assertRaises(CockpitModelError):self.wrapper(112_000_000).call(**self.kw())
        self.assertEqual(len(self.adapter.served),2)
        with Scheduler(self.model.scheduler_db) as scheduler:
            claim=scheduler.connection.execute(f'SELECT * FROM {terminal.CLAIMS}').fetchone()
            self.assertIsNotNone(claim);self.assertTrue(terminal._no_send(scheduler.connection,self.model,claim))
        self.model.config['budget_policy_ref']='memo-test-day-policy';self.raise_policy(3600,140)
        answer=self.wrapper(140_000_000).call(**self.kw());self.assertIsInstance(answer['text'],str)
        self.assertEqual(len(self.adapter.served),3);self.assertEqual(len(self.claim()),2)
        before=self.fees();self.wrapper(140_000_000).call(**self.kw())
        self.assertEqual(len(self.adapter.served),3);self.assertEqual(self.fees(),before)
