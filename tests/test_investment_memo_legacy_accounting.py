import json
import unittest
from unittest.mock import patch
from dataclasses import replace

from tests import test_investment_memo_macro_accounting as fixture
from dalton_core.cockpit_model import CockpitModelError, build_work
from dalton_core.scheduler import Scheduler
from dalton_core.store import canonical_json, content_hash
from dalton_core.investment_memo_macro_accounting import IMPORTS
from dalton_core.investment_memo_legacy_accounting import derive, MAX_NAMESPACE_WORKS


class MemoLegacyAccountingTests(unittest.TestCase):
    setUp = fixture.MemoMacroAccountingTests.setUp
    kw = fixture.MemoMacroAccountingTests.kw
    repair = fixture.MemoMacroAccountingTests.repair
    fees = fixture.MemoMacroAccountingTests.fees
    wrapper = fixture.MemoMacroAccountingTests.wrapper

    def old_groups(self):
        return [self.model.call(**self.kw(i)) for i in range(4)]

    def test_complete_four_groups_and_one_old_semantic_replay_without_new_fees_for_two_companies(self):
        for company in ('company:one','company:two'):
            self.prompts=[fixture.fixture.MemoTimeoutRecoveryTests.kw(self,i,company)['prompt'] for i in range(4)]
            self.old_groups(); paid=self.model.call(**self.repair())
            fees=self.fees(); count=len(self.adapter.served)
            wrapper=self.wrapper(); self.assertTrue(wrapper.macro.complete)
            self.assertFalse(wrapper.macro.scope['complete'])
            wrapper.call(**self.kw(0)); wrapper.prepare_repair(self.repair())
            self.assertTrue(wrapper.can_repair(remaining_groups=3))
            wrapper.begin_stage(max_units=4,allow_technical=False,repair=True)
            free=wrapper.call(**self.repair())
            self.assertEqual(free['work_order_ref'],paid['work_order_ref'])
            self.assertEqual(free['cost_micros'],0)
            self.assertEqual(wrapper.macro.last['historical_work_count'],5)
            self.assertEqual(self.fees(),fees); self.assertEqual(len(self.adapter.served),count)

    def test_multiple_old_semantic_reasons_exhaust_extra_but_remaining_normal_group_can_proceed(self):
        self.model.call(**self.kw(0))
        self.model.call(**self.repair('old first reason')); self.model.call(**self.repair('old second reason'))
        fees=self.fees(); count=len(self.adapter.served)
        wrapper=self.wrapper(); wrapper.call(**self.kw(0)); wrapper.prepare_repair(self.repair('new reason'))
        self.assertEqual(wrapper.macro.last['historical_work_count'],3)
        self.assertEqual(wrapper.macro.extra['role'],'legacy_exhausted')
        self.assertFalse(wrapper.can_repair(remaining_groups=3))
        wrapper.begin_stage(max_units=4,allow_technical=True,repair=True)
        with self.assertRaises(CockpitModelError): wrapper.call(**self.repair('new reason'))
        wrapper.begin_stage(max_units=4,allow_technical=False)
        wrapper=self.wrapper()
        wrapper.call(**self.kw(1))
        self.assertEqual(len(self.adapter.served),count+1)
        self.assertEqual(self.fees()[0][:len(fees[0])],fees[0])
        self.assertEqual(self.fees()[1][:len(fees[1])],fees[1])

    def test_terminal_unsettled_timeout_keeps_original_unknown_reservation_and_allows_only_bounded_extra(self):
        self.model.call(**self.kw(0))
        self.adapter.script={'profile:gpt-6-astra':{'code':'TIMEOUT','message':'unknown timeout'}}
        with patch('dalton_core.cockpit_model._settle_without_losing_the_lease',return_value=None):
            with self.assertRaises(CockpitModelError):self.model.call(**self.kw(1))
        fees=self.fees(); self.adapter.script={}; self.clock.advance(61)
        self.model.call(**self.kw(2)); self.model.call(**self.kw(3))
        fees=self.fees()
        wrapper=self.wrapper(); self.assertTrue(wrapper.macro.complete)
        wrapper.call(**self.kw(0)); wrapper.prepare_repair(self.repair())
        self.assertEqual(wrapper.macro.last['fee_certainty'],'conservative_upper_bound')
        self.assertGreaterEqual(wrapper.accounted_micros,14_000_000)
        self.assertTrue(wrapper.can_repair(remaining_groups=3))
        wrapper.run_micros=14_000_000
        self.assertFalse(wrapper.can_repair(remaining_groups=3))
        self.assertEqual(self.fees(),fees)

    def test_old_ready_work_without_formal_does_not_import_or_dispatch_again(self):
        self.model.call(**self.kw(0)); kwargs=self.kw(1)
        work=build_work(purpose=kwargs['purpose'],request_id=kwargs['request_id'],prompt=kwargs['prompt'],
            mission_version_ref=self.mission['id'],mission_version_hash=self.mission['content_hash'],
            max_input_tokens=600000,max_output_tokens=500,max_cost_usd=14,max_seconds=900,
            created_at=self.clock().isoformat())
        with Scheduler(self.model.scheduler_db) as scheduler: scheduler.enqueue(work)
        count=len(self.adapter.served); fees=self.fees()
        wrapper=self.wrapper(); self.assertFalse(wrapper.macro.complete)
        self.assertIn('in flight',wrapper.macro.import_error)
        self.assertEqual(wrapper.call(**self.kw(0))['cost_micros'],0)
        with self.assertRaises(CockpitModelError):wrapper.call(**self.kw(1))
        self.assertEqual(self.fees(),fees); self.assertEqual(len(self.adapter.served),count)

    def test_new_indexed_terminal_unknown_has_the_same_conservative_gate_and_never_counts_fee_as_zero(self):
        wrapper=self.wrapper();wrapper.call(**self.kw(0))
        self.adapter.script={'profile:gpt-6-astra':{'code':'TIMEOUT','message':'unknown timeout'}}
        with patch('dalton_core.cockpit_model._settle_without_losing_the_lease',return_value=None):
            with self.assertRaises(CockpitModelError):wrapper.call(**self.kw(1))
        fees=self.fees();count=len(self.adapter.served)
        resumed=self.wrapper();resumed.call(**self.kw(0));resumed.prepare_repair(self.repair())
        self.assertIsNone(resumed.macro_error)
        self.assertEqual(resumed.macro.last['fee_certainty'],'conservative_upper_bound')
        self.assertGreaterEqual(resumed.accounted_micros,14_000_000)
        self.assertFalse(resumed.can_repair(remaining_groups=3))
        # Seven retained/future reservations exceed84 once any past fee was
        # measured. A genuinely larger explicit run allowance can cover it;
        # the unknown old admission still has no manufactured settlement.
        resumed.run_micros=98_000_000
        self.assertTrue(resumed.can_repair(remaining_groups=3))
        self.assertEqual(self.fees(),fees);self.assertEqual(len(self.adapter.served),count)

    def test_complete_census_overflow_never_imports_recent_subset(self):
        self.model.call(**self.kw(0))
        work=build_work(purpose='investment_memo',request_id='memo-unrelated',prompt='another input',
            mission_version_ref=self.mission['id'],max_input_tokens=1000,max_output_tokens=500,
            max_cost_usd=14,max_seconds=900,created_at=self.clock().isoformat())
        with Scheduler(self.model.scheduler_db) as scheduler:
            for i in range(MAX_NAMESPACE_WORKS):
                scheduler.enqueue(replace(work,id=f'work:unrelated-memo-{i}',idempotency_key=f'unrelated-memo-{i}',
                    metadata={**work.metadata,'request_id':f'memo-unrelated-{i}'}))
        count=len(self.adapter.served);wrapper=self.wrapper()
        self.assertFalse(wrapper.macro.complete);self.assertIn('pending',wrapper.macro.import_error)
        self.assertEqual(wrapper.call(**self.kw(0))['cost_micros'],0)
        with self.assertRaises(CockpitModelError):wrapper.call(**self.kw(1))
        self.assertEqual(len(self.adapter.served),count)

    def add_unrelated(self,count,*,start=0):
        work=build_work(purpose='investment_memo',request_id='memo-unrelated',prompt='another input',
            mission_version_ref=self.mission['id'],max_input_tokens=1000,max_output_tokens=500,
            max_cost_usd=14,max_seconds=900,created_at=self.clock().isoformat())
        with Scheduler(self.model.scheduler_db) as scheduler:
            for i in range(start,start+count):
                scheduler.enqueue(replace(work,id=f'work:unrelated-memo-{i}',idempotency_key=f'unrelated-memo-{i}',
                    metadata={**work.metadata,'request_id':f'memo-unrelated-{i:08d}'}))

    def test_first_import_over512_completes_all_pages_without_a_paid_call(self):
        self.model.call(**self.kw(0));self.add_unrelated(640)
        fees=self.fees();calls=len(self.adapter.served)
        for i in range(6):
            wrapper=self.wrapper()
            if i<5:
                self.assertFalse(wrapper.macro.complete)
                self.assertIsNotNone(wrapper.macro.pending)
            else:
                self.assertTrue(wrapper.macro.complete)
                self.assertEqual(len(wrapper.macro.legacy['namespace']),641)
        self.assertEqual(wrapper.call(**self.kw(0))['cost_micros'],0)
        self.assertEqual(self.fees(),fees);self.assertEqual(len(self.adapter.served),calls)

    def test_already_imported_replay_survives_other_company_growth_and_incremental_pages(self):
        self.model.call(**self.kw(0));wrapper=self.wrapper();original=wrapper.macro.legacy['content_hash']
        self.add_unrelated(640);fees=self.fees();calls=len(self.adapter.served)
        for i in range(6):
            wrapper=self.wrapper();self.assertTrue(wrapper.macro.complete)
            self.assertEqual(wrapper.macro.legacy['content_hash'],original)
            self.assertEqual(wrapper.call(**self.kw(0))['cost_micros'],0)
            if i<4:self.assertIsNotNone(wrapper.macro.pending)
        self.assertIsNone(wrapper.macro.pending)
        self.assertEqual(self.fees(),fees);self.assertEqual(len(self.adapter.served),calls)

    def test_mission_lane_free_pages_across_restart_reach_real_content_outcome(self):
        from pathlib import Path
        from dalton_core.investment_memo_draft import GROUPS
        from dalton_core.investment_memo_cli import run_memo
        from dalton_core.investment_memo_launcher import InvestmentMemoLauncher
        from dalton_core.mission_investment_memo_lane import MissionInvestmentMemoLaneCoordinator,company_signature
        from dalton_core.lane_child_launcher import write_owner_only
        from dalton_core.investment_memo_macro_accounting import PAGES
        # Real Scheduler, formal/fees, macro owner, CLI group loop, launcher
        # start/status and mission coordinator. Only OS spawning is replaced;
        # its child performs the actual free accounting/CLI path synchronously.
        self.model.call(**self.kw(0));self.add_unrelated(640)
        frozen={'status':'ready','company':{'company_ref':self.company},'mission':self.mission,
            'playbook':{'id':'playbook:one','content_hash':'a'*64,
                'deliverable_templates':{'investment_memo':[f'section{i}' for i in range(12)]},
                'key_questions':[f'question{i}' for i in range(12)]},
            'input_bindings':[],'material':[]}
        outer=self
        class OfflineLauncher(InvestmentMemoLauncher):
            def spawn(inner,*,digest,record,**kwargs):
                path=inner.tickets_dir/digest;path.mkdir(parents=True,exist_ok=True)
                ticket={'id':inner.TICKET_PREFIX+':'+digest,**record,'status':'failed',
                    'pid':0,'started_at':outer.clock().isoformat(),'exit_code':1}
                if (path/'ticket.json').exists():return inner.status(ticket['id'])
                from dalton_core.investment_memo_repair_budget import MemoRepairBudget
                wrapper=MemoRepairBudget(outer.model,run_micros=84_000_000,
                    producer_micros=14_000_000,verifier_micros=14_000_000)
                result=run_memo(store=None,frozen=frozen,model=wrapper,verifier_model=outer.model,
                    max_repairs=0,repair_budget=wrapper)
                if result.get('memo_status')=='macro_accounting_pending':
                    result['memo_input_signature']=company_signature(frozen,inner)
                write_owner_only(path/'ticket.json',ticket);write_owner_only(path/'summary.json',result)
                return inner.status(ticket['id'])
        producer=self.root/'memo-producer.json';verifier=self.root/'memo-verifier.json'
        producer.write_text('{}');verifier.write_text('{}')
        def coordinator():
            launcher=OfflineLauncher(state_dir=self.root,model_config_path=producer,
                verifier_model_config_path=verifier,scheduler_db=self.model.scheduler_db)
            return MissionInvestmentMemoLaneCoordinator(connection=None,launcher=launcher,
                companies=lambda:[self.company],frozen_input=lambda _:frozen)
        def prompt(**kwargs):
            i=[g[0] for g in GROUPS].index(kwargs['group'])
            return self.prompts[i] if not kwargs.get('repair') else self.prompts[i]+'\nYour previous answer for this group was refused and was not used:\n'+str(kwargs['repair'])
        fees=self.fees();calls=len(self.adapter.served);refs=[];lane=coordinator()
        with patch('dalton_core.investment_memo_draft.build_group_prompt',side_effect=prompt):
            for i in range(6):
                if i==2:lane=coordinator()  # writer/coordinator restart
                outcome=lane.dispatch_once();self.assertEqual(outcome['status'],'launched')
                refs.append(outcome['ticket_ref'])
                if i<5:
                    self.assertIsNone(lane.budget.blocked(outcome['signature']))
                    self.assertEqual(lane.launcher.status(refs[-1])['summary']['memo_status'],'macro_accounting_pending')
            # Census complete exposes the actual old malformed answer, not a
            # fake memo success. It then becomes the ordinary content hold.
            self.assertEqual(lane.launcher.status(refs[-1])['summary']['memo_status'],'draft_refused')
            self.assertEqual(lane.dispatch_once()['status'],'held')
        self.assertEqual(len(set(refs)),6)
        with Scheduler(self.model.scheduler_db) as scheduler:
            self.assertEqual(scheduler.connection.execute('SELECT count(*) FROM '+PAGES).fetchone()[0],6)
            self.assertEqual(scheduler.connection.execute('SELECT count(*) FROM '+IMPORTS).fetchone()[0],1)
        self.assertEqual(self.fees(),fees);self.assertEqual(len(self.adapter.served),calls)

    def test_pending_receipt_rejects_wrong_scope_signature_config_and_missing_checkpoint(self):
        from dalton_core.investment_memo_accounting_progress import resume_checkpoint
        self.model.call(**self.kw(0));self.add_unrelated(640)
        wrapper=self.wrapper();progress=wrapper.macro.pending
        producer=self.root/'progress-producer.json';verifier=self.root/'progress-verifier.json'
        producer.write_text('{}');verifier.write_text('{}')
        import hashlib
        ticket={'status':'failed','signature':'company:one|signature','company_ref':self.company,
                'model_config_path':str(producer),'verifier_model_config_path':str(verifier),
                'model_config_fingerprint':hashlib.sha256(producer.read_bytes()).hexdigest(),
                'verifier_model_config_fingerprint':hashlib.sha256(verifier.read_bytes()).hexdigest()}
        summary={'memo_status':'macro_accounting_pending','macro_progress':progress,
                 'memo_input_signature':ticket['signature']}
        self.assertEqual(resume_checkpoint(summary,ticket,self.model.scheduler_db),progress['checkpoint_hash'])
        for key,value in [('company_ref','company:other'),('scope_hash','bad'),('checkpoint_ref','missing')]:
            changed={**progress,key:value};changed.pop('content_hash');changed['content_hash']=content_hash(changed)
            with self.assertRaises(ValueError):
                resume_checkpoint({**summary,'macro_progress':changed},ticket,self.model.scheduler_db)
        with self.assertRaises(ValueError):
            resume_checkpoint({**summary,'memo_input_signature':'foreign'},ticket,self.model.scheduler_db)
        producer.write_text('{"changed":true}')
        with self.assertRaises(ValueError):resume_checkpoint(summary,ticket,self.model.scheduler_db)

    def test_new_matching_repair_on_later_incremental_page_cannot_hide_behind_unrelated_history(self):
        self.model.call(**self.kw(0));wrapper=self.wrapper();self.add_unrelated(300)
        self.model.call(**self.repair('another process'))
        fees=self.fees();calls=len(self.adapter.served)
        # Matching request may sort onto an earlier or later page. Continue
        # bounded scans until either proof is rejected or all history closes.
        for _ in range(4):
            wrapper=self.wrapper();wrapper.call(**self.kw(0))
            if wrapper.macro_error and 'unregistered matching' in wrapper.macro_error:break
        self.assertIn('unregistered matching',wrapper.macro_error)
        wrapper.prepare_repair(self.repair());self.assertFalse(wrapper.can_repair(remaining_groups=3))
        self.assertEqual(self.fees(),fees);self.assertEqual(len(self.adapter.served),calls)

    def test_unregistered_matching_append_after_import_blocks_new_fees_but_keeps_paid_free_answer(self):
        self.model.call(**self.kw(0));wrapper=self.wrapper()
        self.model.call(**self.repair('older process between groups'))
        fees=self.fees();count=len(self.adapter.served)
        self.assertEqual(wrapper.call(**self.kw(0))['cost_micros'],0)
        self.assertIn('unregistered matching request',wrapper.macro_error)
        wrapper.prepare_repair(self.repair());self.assertFalse(wrapper.can_repair(remaining_groups=3))
        with self.assertRaises(CockpitModelError):wrapper.call(**self.kw(1))
        self.assertEqual(self.fees(),fees);self.assertEqual(len(self.adapter.served),count)

    def test_rehashed_import_omitting_old_semantic_is_rejected_by_original_census_rederivation(self):
        self.old_groups();self.model.call(**self.repair());wrapper=self.wrapper()
        with Scheduler(self.model.scheduler_db) as scheduler, scheduler._transaction() as cur:
            row=cur.execute('SELECT * FROM '+IMPORTS).fetchone();record=json.loads(row['record_json'])
            record['historical_requests']=[e for e in record['historical_requests'] if e['role']!='semantic']
            record['extra']=None;record.pop('content_hash');record['content_hash']=content_hash(record)
            cur.execute('DROP TRIGGER '+IMPORTS+'_update_immutable')
            cur.execute('UPDATE '+IMPORTS+' SET record_json=?,content_hash=?',
                        (canonical_json(record),content_hash(record)))
        count=len(self.adapter.served);broken=self.wrapper()
        self.assertFalse(broken.macro.complete)
        self.assertIn('complete original membership',broken.macro.import_error)
        self.assertEqual(broken.call(**self.kw(0))['cost_micros'],0)
        broken.prepare_repair(self.repair());self.assertFalse(broken.can_repair(remaining_groups=3))
        self.assertEqual(len(self.adapter.served),count)

    def test_namespace_cas_difference_does_not_write_a_partial_import(self):
        self.model.call(**self.kw(0));proof=derive(self.model,self.prompts)
        proof['namespace']=proof['namespace'][:-1]
        with patch('dalton_core.investment_memo_legacy_accounting.derive',return_value=proof):wrapper=self.wrapper()
        self.assertFalse(wrapper.macro.complete);self.assertIn('changed before owner import',wrapper.macro.import_error)
        with Scheduler(self.model.scheduler_db) as scheduler:
            self.assertEqual(scheduler.connection.execute('SELECT count(*) FROM '+IMPORTS).fetchone()[0],0)

    def test_old_independent_verifier_is_indexed_and_freely_reused_with_exact_producer_routes(self):
        from tests import test_cockpit_model_fallback as models
        from dalton_core.cockpit_model import independent_model_call
        from dalton_core.investment_memo_repair_budget import MemoMacroVerifier
        normal=self.old_groups(); refs=[c['route_decision_ref'] for c in normal]
        adapter=models.ChainAdapter({})
        verifier=models.CockpitChainTests._model(self,adapter,policy_version_ref=self.verifier_policy,
                                                slots=self.verifier_slots,clock=self.clock)
        verifier.config['budget_policy_ref']='memo-test-day-policy'
        kwargs={'purpose':'investment_memo_verifier','request_id':'memo-verify-'+content_hash(refs)[:24],
                'prompt':'Independently verify the four frozen groups','mission':self.mission}
        paid=independent_model_call(verifier,producer_route_decision_refs=refs,**kwargs)
        fees=self.fees();count=len(adapter.served)
        wrapper=self.wrapper();wrapper.call(**self.kw(0))
        free=MemoMacroVerifier(verifier,wrapper).call(producer_route_decision_refs=refs,**kwargs)
        self.assertEqual(free['work_order_ref'],paid['work_order_ref']);self.assertEqual(free['cost_micros'],0)
        self.assertEqual(wrapper.macro.last['historical_work_count'],5)
        self.assertEqual(self.fees(),fees);self.assertEqual(len(adapter.served),count)
        with self.assertRaises(CockpitModelError):
            MemoMacroVerifier(verifier,wrapper).call(producer_route_decision_refs=refs[:1],**kwargs)

    def test_verifier_added_by_old_process_after_import_cannot_escape_macro_fee_index(self):
        from tests import test_cockpit_model_fallback as models
        from dalton_core.cockpit_model import independent_model_call
        normal=self.old_groups();wrapper=self.wrapper()
        refs=[c['route_decision_ref'] for c in normal]
        adapter=models.ChainAdapter({})
        verifier=models.CockpitChainTests._model(self,adapter,policy_version_ref=self.verifier_policy,
                                                slots=self.verifier_slots,clock=self.clock)
        verifier.config['budget_policy_ref']='memo-test-day-policy'
        kwargs={'purpose':'investment_memo_verifier','request_id':'memo-verify-'+content_hash(refs)[:24],
                'prompt':'Independently verify the four frozen groups','mission':self.mission}
        independent_model_call(verifier,producer_route_decision_refs=refs,**kwargs)
        fees=self.fees();calls=len(self.adapter.served)
        wrapper.call(**self.kw(0))
        self.assertIn('unregistered matching',wrapper.macro_error)
        wrapper.prepare_repair(self.repair());self.assertFalse(wrapper.can_repair(remaining_groups=3))
        self.assertEqual(self.fees(),fees);self.assertEqual(len(self.adapter.served),calls)

    def test_old_paid_technical_child_is_reused_and_consumes_extra_without_refunding_parent(self):
        from dalton_core.investment_memo_repair_budget import MemoRepairBudget
        def old_wrapper():
            w=MemoRepairBudget(self.model,run_micros=84_000_000,
                producer_micros=14_000_000,verifier_micros=14_000_000)
            w.begin_stage(max_units=4,allow_technical=True)
            return w
        before=old_wrapper();before.call(**self.kw(0));before.call(**self.kw(1))
        self.adapter.script={'profile:gpt-6-astra':{'code':'TIMEOUT','message':'old timeout'}}
        with self.assertRaises(CockpitModelError):before.call(**self.kw(2))
        self.adapter.script={};self.clock.advance(61)
        resumed=old_wrapper();resumed.call(**self.kw(0));resumed.call(**self.kw(1))
        child=resumed.call(**self.kw(2));resumed.call(**self.kw(3))
        fees=self.fees();count=len(self.adapter.served)
        wrapper=self.wrapper();free=wrapper.call(**self.kw(2))
        self.assertEqual(free['work_order_ref'],child['work_order_ref']);self.assertEqual(free['cost_micros'],0)
        self.assertEqual(wrapper.macro.extra['role'],'legacy_exhausted')
        wrapper.prepare_repair(self.repair());self.assertFalse(wrapper.can_repair(remaining_groups=1))
        self.assertGreaterEqual(wrapper.accounted_micros,14_000_000)
        self.assertEqual(self.fees(),fees);self.assertEqual(len(self.adapter.served),count)


if __name__=='__main__':unittest.main()
