"""Real Scheduler/router/fee transport for bounded memo content maintenance."""
import copy
import json
import unittest
from dataclasses import replace
from types import SimpleNamespace
from unittest.mock import patch

from dalton_core.cockpit_model import CockpitModelError
from dalton_core.investment_memo_cli import run_memo, _validated_group
from dalton_core.investment_memo_draft import GROUPS, build_group_prompt, draft_group
from dalton_core.investment_memo_group_maintenance import TABLE, CONTRACT_REF, derive
from dalton_core.investment_memo_macro_accounting import EXTRA, REQUESTS, request_id
from dalton_core.investment_memo_repair_budget import MemoRepairBudget
from dalton_core.scheduler import Scheduler
from dalton_core.store import content_hash
from tests import test_investment_memo_timeout_recovery as transport
from tests import test_investment_memo_draft as payloads


class MemoGroupMaintenanceTests(unittest.TestCase):
    def setUp(self):
        transport.MemoTimeoutRecoveryTests.setUp(self)
        self.mission['autonomy']={'automation_principal':'worker:research'}
        self.contexts = self.make_contexts('company:synthetic-a')
        self.prompts = [build_group_prompt(**context) for context in self.contexts]
        self.bad_groups = {0,1}
        self.bad_maintenance = set()
        self.bad_old_repair = False
        self.seen = []
        execute = self.adapter.execute
        def answer(work, route, profile):
            invocation,envelope = execute(work,route,profile)
            self.seen.append(work)
            if envelope.status != 'succeeded':return invocation,envelope
            if work.metadata['purpose']=='investment_memo_verifier':
                contract=json.loads(work.question.split('Return raw JSON only:\n',1)[1].split('\n',1)[0])
                value={'verdict':'pass','verified_body_hash':contract['verified_body_hash'],'finding_codes':[]}
            else:
                from dalton_core.investment_memo_timeout_recovery import prompt_identity
                _,_,index,_=prompt_identity(work.question)
                value=payloads.group_payload(self.contexts[index]['section_titles'],self.contexts[index]['questions'])
                maintenance='\nMaintenance contract: '+CONTRACT_REF in work.question
                repair='\nYour previous answer for this group was refused and was not used:\n' in work.question
                bad=(index in self.bad_maintenance if maintenance else self.bad_old_repair if repair else index in self.bad_groups)
                if bad:
                    value['sections'][-1]['body']+=' 未绑定的收入 888.1。'
                    value['sections'][0]['body']+=' 未绑定的利润 777.2。'
            return invocation,replace(envelope,outputs={'text':json.dumps(value,ensure_ascii=False)})
        self.adapter.execute=answer

    def make_contexts(self, company):
        questions=[{'question_ref':f'memo_q{i:02d}','question':q} for i,q in enumerate(payloads.QUESTIONS,1)]
        return [dict(group=group,section_titles=[payloads.TITLES[i] for i in si],
                     questions=[questions[i] for i in qi],
                     material=[{'ref':'claim-version:1','kind':'claim','text':'收入增长已核对。'}],
                     company={'company_ref':company}) for group,si,qi in GROUPS]

    def wrapper(self, run=84_000_000):
        wrapper=MemoRepairBudget(self.model,run_micros=run,producer_micros=14_000_000,verifier_micros=14_000_000)
        wrapper.bind_macro(self.prompts,self.mission,allow_maintenance=True)
        wrapper.begin_stage(max_units=4,allow_technical=False)
        return wrapper

    def kw(self,index):
        return {'purpose':'investment_memo','request_id':request_id(GROUPS[index][0],self.prompts[index]),
                'prompt':self.prompts[index],'mission':self.mission}

    def outcome(self,wrapper,index):
        wrapper.begin_stage(max_units=4,allow_technical=False)
        return _validated_group(SimpleNamespace(),draft_group(wrapper,**self.contexts[index],mission=self.mission),
                                self.contexts[index]['material'])

    def old_repair(self,wrapper,index=0):
        prompt=build_group_prompt(**self.contexts[index],repair={'reason':'old diagnostic','refs':[]})
        kwargs={**self.kw(index),'prompt':prompt,'request_id':request_id(GROUPS[index][0],prompt)}
        wrapper.prepare_repair(kwargs)
        self.assertTrue(wrapper.can_repair(remaining_groups=3-index))
        wrapper.begin_stage(max_units=4,allow_technical=False,repair=True)
        return wrapper.call(**kwargs)

    def fees(self):return transport.MemoTimeoutRecoveryTests.fee_rows(self)

    def seeded(self):
        wrapper=self.wrapper();self.outcome(wrapper,0);self.old_repair(wrapper)
        outcome=self.outcome(wrapper,1)
        self.assertEqual(outcome['failure_class'],'content')
        return wrapper,outcome

    def purchase(self,wrapper,outcome,index=1):
        body=wrapper.prepare_maintenance(context=self.contexts[index],outcome=outcome,remaining_groups=3-index)
        self.assertIsNotNone(body)
        wrapper.begin_stage(max_units=4,allow_technical=False,maintenance=body)
        value=_validated_group(SimpleNamespace(),draft_group(wrapper,**self.contexts[index],mission=self.mission,
                               maintenance=body),self.contexts[index]['material'])
        return body,value

    def test_later_group_after_original_repair_has_one_fixed_permission_and_free_restart(self):
        for company in ('company:synthetic-a','company:synthetic-b'):
            self.contexts=self.make_contexts(company);self.prompts=[build_group_prompt(**c) for c in self.contexts]
            wrapper,outcome=self.seeded();old=self.fees();count=len(self.adapter.served)
            body,value=self.purchase(wrapper,outcome)
            self.assertEqual(value['status'],'drafted');self.assertEqual(len(self.adapter.served),count+1)
            self.assertIn('888.1',body['prompt']);self.assertIn('777.2',body['prompt'])
            self.assertIn(outcome['raw_response'],body['prompt'])
            self.assertEqual(len(body['diagnostics']['findings']),2)
            new=self.fees();self.assertEqual(new[0][:len(old[0])],old[0]);self.assertEqual(new[1][:len(old[1])],old[1])
            fresh=self.wrapper();paid=len(self.adapter.served);bills=self.fees()
            self.assertEqual(self.outcome(fresh,0)['status'],'drafted') # old repair, not bad normal
            self.assertEqual(self.outcome(fresh,1)['status'],'drafted') # maintenance replay
            self.assertEqual(len(self.adapter.served),paid);self.assertEqual(self.fees(),bills)
            self.assertEqual(fresh.macro.last['historical_work_count'],4)
            self.assertEqual(fresh.macro.ref,wrapper.macro.ref)
            self.assertEqual(body['permission_ref'],wrapper.macro.ref+':group-maintenance:1')

    def test_bad_maintenance_cannot_purchase_again_on_restart_or_changed_diagnostic(self):
        wrapper,outcome=self.seeded();self.bad_maintenance={1};body,bad=self.purchase(wrapper,outcome)
        self.assertEqual(bad['status'],'refused');old=self.fees();count=len(self.adapter.served)
        for _ in range(3):
            fresh=self.wrapper();again=self.outcome(fresh,1)
            self.assertEqual(again['status'],'refused')
            self.assertIsNone(fresh.prepare_maintenance(context=self.contexts[1],outcome=again,remaining_groups=2))
        self.assertEqual(len(self.adapter.served),count);self.assertEqual(self.fees(),old)
        with Scheduler(self.model.scheduler_db) as s:
            self.assertEqual(s.connection.execute(f'SELECT count(*) FROM {TABLE}').fetchone()[0],1)
            self.assertEqual(s.connection.execute(f'SELECT count(*) FROM {EXTRA}').fetchone()[0],1)

    def test_existing_technical_slot_does_not_disable_content_maintenance_for_another_group(self):
        wrapper=self.wrapper();self.outcome(wrapper,0);self.outcome(wrapper,1)
        self.adapter.script={'profile:gpt-6-astra':{'code':'TIMEOUT','message':'host completion exceeded request timeout'}}
        wrapper.begin_stage(max_units=4,allow_technical=False)
        with self.assertRaises(CockpitModelError):wrapper.call(**self.kw(2))
        self.clock.advance(61);self.adapter.script={}
        fresh=self.wrapper();fresh.begin_stage(max_units=4,allow_technical=True)
        fresh.call(**self.kw(0));fresh.call(**self.kw(1));fresh.call(**self.kw(2))
        outcome=self.outcome(fresh,1)
        body,result=self.purchase(fresh,outcome)
        self.assertEqual(result['status'],'drafted');self.assertTrue(fresh.technical_slot_consumed)
        self.assertEqual(fresh.macro.extra['role'],'technical')
        self.assertGreaterEqual(fresh.macro.last['accounted_micros'],14_000_000)

    def test_unknown_future_fee_is_retained_and_insufficient_run_headroom_denies_maintenance(self):
        wrapper,outcome=self.seeded()
        self.adapter.script={'profile:gpt-6-astra':{'code':'TIMEOUT','message':'unknown'}}
        with patch('dalton_core.cockpit_model._settle_without_losing_the_lease',return_value=None):
            wrapper.begin_stage(max_units=4,allow_technical=False)
            with self.assertRaises(CockpitModelError):wrapper.call(**self.kw(3))
        self.adapter.script={};old=self.fees();paid=len(self.adapter.served)
        fresh=self.wrapper(run=42_000_000)
        self.assertIsNone(fresh.prepare_maintenance(context=self.contexts[1],outcome=outcome,remaining_groups=2))
        self.assertEqual(len(self.adapter.served),paid);self.assertEqual(self.fees(),old)
        self.assertTrue(fresh.macro.last['unknown_fee_retained_work_refs'])
        self.assertGreaterEqual(fresh.macro.last['accounted_micros'],14_000_000)

    def test_parent_source_company_mission_or_forged_diagnostics_cannot_authorize(self):
        wrapper,outcome=self.seeded();count=len(self.adapter.served)
        for key,value in (('company',{'company_ref':'company:foreign'}),('material',[])):
            foreign=copy.deepcopy(self.contexts[1]);foreign[key]=value
            self.assertIsNone(wrapper.prepare_maintenance(context=foreign,outcome=outcome,remaining_groups=2))
        foreignmission={**self.mission,'id':'mission:other','content_hash':'a'*64}
        wrapper.macro.mission=foreignmission
        self.assertIsNone(wrapper.prepare_maintenance(context=self.contexts[1],outcome=outcome,remaining_groups=2))
        wrapper.macro.mission=self.mission
        body=wrapper.prepare_maintenance(context=self.contexts[1],outcome=outcome,remaining_groups=2)
        body['diagnostics']['findings']=[]
        with self.assertRaisesRegex(ValueError,'diagnostics/source'):
            wrapper.macro.register_maintenance(body,{'purpose':'investment_memo','request_id':body['entry']['request_id'],
                                                  'prompt':body['prompt'],'mission':self.mission})
        self.assertEqual(len(self.adapter.served),count)

    def test_absent_original_quotes_do_not_get_maintenance_even_with_paid_success(self):
        for context in self.contexts:
            context['material'].append({'ref':'doc-passage:alphaengine-doc:123:0-8:000000000000',
                                       'kind':'pack_passage','text':'missing original'})
        self.prompts=[build_group_prompt(**c) for c in self.contexts]
        wrapper,outcome=self.seeded();old=self.fees();count=len(self.adapter.served)
        self.assertIsNone(wrapper.prepare_maintenance(context=self.contexts[1],outcome=outcome,remaining_groups=2))
        self.assertEqual(self.fees(),old);self.assertEqual(len(self.adapter.served),count)

    def test_parent_provider_failure_or_inflight_formal_is_not_content_permission(self):
        wrapper,outcome=self.seeded()
        self.adapter.script={'profile:gpt-6-astra':{'code':'TIMEOUT','message':'host timeout'}}
        wrapper.begin_stage(max_units=4,allow_technical=False)
        with self.assertRaises(CockpitModelError):wrapper.call(**self.kw(3))
        with self.assertRaises((CockpitModelError,ValueError)):
            derive(wrapper.macro,self.contexts[3],self.seen[-1].id)
        self.adapter.script={}
        # A strong Work with no formal result is never a successful draft.
        from dalton_core.cockpit_model import build_work
        work=build_work(purpose='investment_memo',request_id=self.kw(2)['request_id'],prompt=self.prompts[2],
            mission_version_ref=self.mission['id'],mission_version_hash=self.mission['content_hash'],
            max_input_tokens=120000,max_output_tokens=500,max_cost_usd=.5,max_seconds=180,
            created_at=self.clock().isoformat())
        with Scheduler(self.model.scheduler_db) as s:s.enqueue(work)
        with self.assertRaises(CockpitModelError):derive(wrapper.macro,self.contexts[2],work.id)

    def test_original_zero_repair_setting_still_never_uses_new_permission(self):
        wrapper,outcome=self.seeded();paid=len(self.adapter.served)
        result,_=self.full_run(max_repairs=0)
        self.assertEqual(result['status'],'refused');self.assertEqual(len(self.adapter.served),paid)
        with Scheduler(self.model.scheduler_db) as s:
            self.assertEqual(s.connection.execute(f'SELECT count(*) FROM {TABLE}').fetchone()[0],0)

    def test_fixed_permission_is_owner_append_only_and_cannot_switch_parent_or_frozen_source(self):
        wrapper,outcome=self.seeded();body,result=self.purchase(wrapper,outcome)
        with Scheduler(self.model.scheduler_db) as scheduler:
            with self.assertRaisesRegex(Exception,'append-only'):
                scheduler.connection.execute(f'UPDATE {TABLE} SET content_hash=?',('0'*64,))
            with self.assertRaisesRegex(Exception,'Scheduler owner'):
                scheduler.connection.execute(f'INSERT INTO {TABLE} VALUES (?,?,?,?)',
                    ('forged',wrapper.macro.ref,'{}',content_hash({})))
        # Even a writer-tampered but internally canonical record cannot bypass
        # recomputation of its exact formal parent or current frozen sources.
        from dalton_core.store import canonical_json
        with Scheduler(self.model.scheduler_db) as scheduler,scheduler._transaction() as cur:
            cur.execute(f'DROP TRIGGER {TABLE}_update_immutable')
            altered=copy.deepcopy(body);altered['raw_response_hash']='0'*64
            cur.execute(f'UPDATE {TABLE} SET record_json=?,content_hash=?',
                        (canonical_json(altered),content_hash(altered)))
        old=self.fees();count=len(self.adapter.served)
        fresh=self.wrapper()
        with self.assertRaisesRegex(CockpitModelError,'no longer replay'):
            fresh.call(**self.kw(1))
        self.assertEqual(self.fees(),old);self.assertEqual(len(self.adapter.served),count)

    def test_same_permission_under_changed_mission_never_borrows_old_paid_grant(self):
        wrapper,outcome=self.seeded();body,value=self.purchase(wrapper,outcome)
        old=self.fees();count=len(self.adapter.served)
        self.mission={**self.mission,'id':'coverage-mission-version:new','content_hash':'f'*64}
        fresh=self.wrapper()
        self.assertEqual(fresh.macro.ref,wrapper.macro.ref)
        with self.assertRaisesRegex(CockpitModelError,'another mission'):
            fresh.call(**self.kw(1))
        self.assertEqual(self.fees(),old);self.assertEqual(len(self.adapter.served),count)
        self.assertGreaterEqual(fresh.macro.snapshot()[2],wrapper.macro.snapshot()[2])

    def test_old_runtime_counts_new_fee_or_rejects_at_its_original_six_request_bound(self):
        wrapper,outcome=self.seeded();body,value=self.purchase(wrapper,outcome)
        # This is the actual frozen product module, not a mirror assertion.
        from pathlib import Path
        import hashlib
        import dalton_core.investment_memo_macro_accounting as accounting
        fixture=json.loads((Path(__file__).parent/'fixtures'/'memo_macro_original_snapshot.json').read_text())
        source=fixture['method']
        self.assertEqual(hashlib.sha256(source.encode()).hexdigest(),fixture['method_sha256'])
        namespace=dict(vars(accounting))
        exec(compile(source,'frozen_macro_0_1_snapshot.py','exec'),namespace)
        legacy_class=type('OriginalSnapshot',(accounting.MemoMacroAccounting,),{'snapshot':namespace['snapshot']})
        old=legacy_class.__new__(legacy_class);old.__dict__=dict(wrapper.macro.__dict__)
        self.assertEqual(old.snapshot()[2],wrapper.macro.snapshot()[2])
        self.assertEqual(old.last['historical_work_count'],4)
        # A second proved maintenance produces >6 rows; the old runtime must
        # refuse rather than silently omit them from its cost calculation.
        # The successful first repair is already re-used; a third-group
        # defect consumes its own fixed maintenance permission.
        self.bad_groups.add(2);outcome=self.outcome(wrapper,2)
        body,value=self.purchase(wrapper,outcome,index=2)
        with self.assertRaisesRegex(ValueError,'six bounded'):
            old.snapshot()

    def test_same_group_concurrent_registration_is_one_immutable_permission(self):
        wrapper,outcome=self.seeded()
        body=wrapper.prepare_maintenance(context=self.contexts[1],outcome=outcome,remaining_groups=2)
        kwargs={'purpose':'investment_memo','request_id':body['entry']['request_id'],
                'prompt':body['prompt'],'mission':self.mission}
        from concurrent.futures import ThreadPoolExecutor
        # Keep the real long-lived owner open, as the production writer does.
        with Scheduler(self.model.scheduler_db),ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(lambda _:wrapper.macro.register_maintenance(body,kwargs),range(2)))
        with Scheduler(self.model.scheduler_db) as scheduler:
            self.assertEqual(scheduler.connection.execute(f'SELECT count(*) FROM {TABLE}').fetchone()[0],1)
            self.assertEqual(scheduler.connection.execute(f'SELECT count(*) FROM {REQUESTS} '
                "WHERE json_extract(record_json,'$.role')='maintenance'").fetchone()[0],1)

    def test_normal_day_pool_denial_cannot_be_bypassed_by_fixed_permission(self):
        wrapper,outcome=self.seeded();self.mission['budget']['max_daily_paid_calls']=3
        old=self.fees();count=len(self.adapter.served)
        body,value=self.purchase(wrapper,outcome)
        self.assertEqual(value['status'],'refused');self.assertNotEqual(value.get('failure_class'),'content')
        self.assertEqual(self.fees(),old);self.assertEqual(len(self.adapter.served),count)
        self.assertIsNone(wrapper.prepare_maintenance(context=self.contexts[1],outcome=value,remaining_groups=2))
        with Scheduler(self.model.scheduler_db) as scheduler:
            self.assertEqual(scheduler.connection.execute(f'SELECT count(*) FROM {TABLE}').fetchone()[0],1)

    def test_all_parse_citation_and_section_failures_are_kept_without_accepting_partial_sections(self):
        wrapper=self.wrapper()
        value=payloads.group_payload(self.contexts[1]['section_titles'],self.contexts[1]['questions'])
        value['sections'][0]['claim_refs']=['claim-version:invented']
        value['sections'][-1]['body']+=' 未绑定的收入 987.3。'
        execute=self.adapter.execute
        def bad(work,route,profile):
            invocation,envelope=execute(work,route,profile)
            return invocation,replace(envelope,outputs={'text':json.dumps(value,ensure_ascii=False)})
        self.adapter.execute=bad
        outcome=self.outcome(wrapper,1)
        self.assertEqual(outcome['status'],'refused')
        codes={row['code'] for row in outcome['findings']}
        self.assertEqual(codes,{'citation_slot','section_authority'})
        self.assertTrue(any('987.3' in row['detail'] for row in outcome['findings']))
        self.assertNotIn('sections',outcome)

    def test_caller_cannot_skip_original_slot_or_buy_maintenance_from_unexecuted_intent(self):
        wrapper=self.wrapper();outcome=self.outcome(wrapper,1)
        with self.assertRaisesRegex(ValueError,'extra permission'):
            derive(wrapper.macro,self.contexts[1],outcome['model']['work_order_ref'])
        prompt=build_group_prompt(**self.contexts[0],repair={'reason':'old diagnostic'})
        kwargs={**self.kw(0),'prompt':prompt,'request_id':request_id(GROUPS[0][0],prompt)}
        wrapper.macro.register(role='semantic',kwargs=kwargs)
        with self.assertRaisesRegex(ValueError,'awaits dispatch'):
            derive(wrapper.macro,self.contexts[1],outcome['model']['work_order_ref'])
        self.assertEqual(len(self.adapter.served),1)

    def test_zero_policy_preserves_completed_free_replay_but_cannot_dispatch_pending_maintenance(self):
        wrapper,outcome=self.seeded()
        body=wrapper.prepare_maintenance(context=self.contexts[1],outcome=outcome,remaining_groups=2)
        kwargs={'purpose':'investment_memo','request_id':body['entry']['request_id'],
                'prompt':body['prompt'],'mission':self.mission}
        wrapper.macro.register_maintenance(body,kwargs) # crash before paid dispatch
        old=self.fees();count=len(self.adapter.served)
        fresh=self.wrapper();fresh.maintenance_enabled=False
        with self.assertRaisesRegex(CockpitModelError,'disabled'):
            fresh.call(**self.kw(1))
        self.assertEqual(self.fees(),old);self.assertEqual(len(self.adapter.served),count)
        # Existing permission under normal policy can still finish exactly once.
        done=self.wrapper();value=self.outcome(done,1)
        self.assertEqual(value['status'],'drafted')
        old=self.fees();count=len(self.adapter.served)
        free=self.wrapper();free.maintenance_enabled=False
        self.assertEqual(self.outcome(free,1)['status'],'drafted')
        self.assertEqual(self.fees(),old);self.assertEqual(len(self.adapter.served),count)

    def test_complete_legacy_multiple_repairs_keep_exhausted_slot_and_all_old_bills(self):
        # Pre-owner historical attempts are imported as legacy_exhausted. The
        # new group permission cannot reset them or pretend they never existed.
        self.model.call(**self.kw(0))
        for reason in ('old diagnosis one','old diagnosis two'):
            prompt=build_group_prompt(**self.contexts[0],repair={'reason':reason,'refs':[]})
            self.model.call(**{**self.kw(0),'prompt':prompt,'request_id':request_id(GROUPS[0][0],prompt)})
        self.model.call(**self.kw(1));old=self.fees();count=len(self.adapter.served)
        wrapper=self.wrapper();outcome=self.outcome(wrapper,1)
        self.assertEqual(wrapper.macro.extra['role'],'legacy_exhausted')
        body,result=self.purchase(wrapper,outcome)
        self.assertEqual(body['original_extra']['role'],'legacy_exhausted')
        self.assertEqual(result['status'],'drafted');self.assertEqual(len(self.adapter.served),count+1)
        new=self.fees();self.assertEqual(new[0][:len(old[0])],old[0]);self.assertEqual(new[1][:len(old[1])],old[1])
        fresh=self.wrapper();self.assertEqual(self.outcome(fresh,1)['status'],'drafted')
        self.assertEqual(fresh.macro.last['historical_work_count'],5)
        self.assertEqual(fresh.macro.extra,wrapper.macro.extra)
        self.assertEqual(len(self.adapter.served),count+1)

    def full_run(self,max_repairs=1):
        frozen={'status':'ready','mission':self.mission,
                'playbook':{'id':'playbook:test','content_hash':'a'*64,
                  'deliverable_templates':{'investment_memo':payloads.TITLES},'key_questions':payloads.QUESTIONS},
                'company':self.contexts[0]['company'],'material':self.contexts[0]['material'],
                'input_bindings':[{'ref':'claim-version:1','kind':'claim','hash':'b'*64}], 'material_pack':{}}
        wrapper=self.wrapper()
        # The actual independent verifier uses another family and the same fee authority.
        from tests import test_cockpit_model_fallback as fixture
        verifier=fixture.CockpitChainTests._model(self,self.adapter,policy_version_ref=self.verifier_policy,
                                                 slots=self.verifier_slots,clock=self.clock)
        verifier.config['budget_policy_ref']='memo-test-day-policy'
        verifier.config['purpose_call_budgets']['investment_memo_verifier']=dict(
            max_input_tokens=120000,max_output_tokens=500,max_cost_usd=.5,timeout_seconds=180)
        with patch('dalton_core.investment_memo_cli.MissionDeliverableAuthority') as authority,\
             patch('dalton_core.research_product_quality.product_quality',return_value={'status':'test'}):
            # Publication ownership has its own tests; transport, all group
            # parsers/section validators and independent verifier remain real.
            authority.return_value.publish.return_value={'id':'memo:test','content_hash':'c'*64,'status':'candidate'}
            result=run_memo(store=SimpleNamespace(connection=None),frozen=frozen,model=wrapper,
                            verifier_model=verifier,max_repairs=max_repairs,repair_budget=wrapper)
        return result,wrapper

    def test_real_four_group_run_reuses_old_successes_then_maintenance_and_independent_verifier(self):
        result,first=self.full_run()
        self.assertEqual(result['status'],'succeeded',result)
        self.assertEqual(len(result['repairs']) if 'repairs' in result else len(first.completed_calls),6)
        self.assertEqual(len(self.adapter.served),7) # 4 groups, old repair, maintenance, independent verifier
        fees=self.fees();paid=len(self.adapter.served)
        result,second=self.full_run()
        self.assertEqual(result['status'],'succeeded',result);self.assertEqual(result['cost_micros'],0)
        self.assertEqual(self.fees(),fees);self.assertEqual(len(self.adapter.served),paid)
        self.assertEqual(second.accounted_micros,first.accounted_micros)
        self.assertEqual(second.macro.last['historical_work_count'],7)
