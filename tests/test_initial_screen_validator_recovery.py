"""Actual Scheduler/formal/route/fees, with isolated original-source fixtures."""
import copy,json,os,hashlib
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest import mock
from tests import test_initial_screen_quiesce as q
from tests.synthetic_company import strl_worksheet,strl_document
from dalton_core import initial_screen_validator_recovery as rec,initial_screen_failure_feedback as feedback
from dalton_core import initial_screen_failed_input as guard,initial_screen_cli as cli
from dalton_core.initial_screen import build_worksheet_prompt,build_document_prompt,build_verifier_prompt
from dalton_core.initial_screen import pack_resolvers
from dalton_core.product_contract import tag_entry
from dalton_core.store import content_hash

class ValidatorRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.q=q.InitialScreenQuiesceTests('runTest');self.q.setUp();self.addCleanup(self.q.doCleanups)
        self.f=self.q.fixture;self.model=self.f.model;self.state=self.f.root;self.context=self.q.context
        self.context['entry']['reopen']=None
        def resolved(state,current,purpose):
            if purpose=='initial_screen_verifier':
                return {**current,'routing_policy_ref':self.f.verifier_policy,'credential_slot_refs':self.f.verifier_slots}
            return current
        self.resolved=mock.patch('dalton_core.host_research_reconcile.resolved_model_config',side_effect=resolved)
        self.resolved.start();self.addCleanup(self.resolved.stop)
        self.base=rec._base(self.f.mission,self.context)
        self.binding=guard.input_binding(self.f.mission,self.f.company,self.f.pack,None,{},execution=guard.execution_binding(self.model,self.state))
        self.previous={'input_hash':self.binding['content_hash'],'source_binding_hash':content_hash({}),
            'failure_reason':'old validator refused','terminal_status':'failed','validation_contract':self.binding['validation_contract'],
            'source_ticket_ref':self.q.ticket['id']}
        proxy=self.q.proxy()
        self.f.adapter.value=strl_worksheet()
        proxy.call(purpose='initial_screen',request_id=self.base+':worksheet',prompt=feedback.validation_prompt(build_worksheet_prompt(company=self.f.company,pack=self.f.pack),self.context),mission=self.f.mission)
        self.f.adapter.value=strl_document()
        proxy.call(purpose='initial_screen',request_id=self.base+':document:0',prompt=feedback.validation_prompt(build_document_prompt(company=self.f.company,pack=self.f.pack,worksheet=strl_worksheet()),self.context),mission=self.f.mission)
        feedback._write(self.q.path/'ticket.json',{**self.q.ticket,'status':'failed','exit_code':1,'completed_at':'2026-10-11T17:01:00+00:00'})
        feedback._write(self.q.path/'summary.json',{'status':'failed','drafted':self.f.company,'failure_reason':'old validator refused'})
        guard.record_failure(self.state,{'status':'failed','_failed_input_binding':self.binding,'_failed_source_ticket_ref':self.q.ticket['id'],'failure_reason':'old validator refused'})
        # These isolated fixtures exercise real deterministic section checks;
        # acquisition-specific original proof is independently covered by the
        # existing feedback source-version/tamper tests, not fabricated here.
        numbers,passages=pack_resolvers(self.f.pack)
        self.sources=mock.patch.object(rec,'_source_resolvers',return_value=({entry['ref'] for tag in self.f.pack['tags'] if (entry:=tag_entry(self.f.pack,tag)) and entry['kind']=='claim'},lambda x:True,lambda ref:passages.get(ref),{'isolated-original-proof':'held'}))
        self.sources.start();self.addCleanup(self.sources.stop)
        self.original=self.f.adapter.execute
        self.f.adapter.value={'verdict':'pass','findings':[]}
    def qualify(self):
        return rec.qualify(state=self.state,binding=self.binding,previous=self.previous,mission=self.f.mission,model=self.model,authority=self.f.authority)
    def test_paid_original_free_recheck_then_only_unexecuted_verifier_and_restart(self):
        before=len(self.f.adapter.served);r=self.qualify();self.assertEqual(r['record']['findings'],[])
        saved=rec.persist(self.state,r)
        verdict,call=rec.verifier(r,model=self.model,mission=self.f.mission,state=self.state)
        self.assertEqual(verdict['verdict'],'pass');self.assertEqual(len(self.f.adapter.served),before+1)
        self.assertTrue(any(work['metadata']['request_id'].startswith(self.base+':verify:0') for work in rec._works(self.model,self.base)))
        restarted=self.qualify();self.assertEqual(saved,rec.persist(self.state,restarted))
        verdict,replay=rec.verifier(restarted,model=self.model,mission=self.f.mission,state=self.state)
        self.assertEqual(replay['work_order_ref'],call['work_order_ref']);self.assertEqual(len(self.f.adapter.served),before+1)
    def test_real_content_missing_stays_rejected_and_current_findings_reach_planner(self):
        # Source removal is a genuine current evidence failure, not a parser fix.
        self.sources.stop()
        with mock.patch.object(rec,'_source_resolvers',return_value=(set(),lambda x:False,lambda ref:None,{})):
            r=self.qualify();self.assertTrue(r['record']['findings']);rec.persist(self.state,r)
            before=len(self.f.adapter.served)
            with self.assertRaisesRegex(ValueError,'deterministic'):rec.verifier(r,model=self.model,mission=self.f.mission,state=self.state)
            value=feedback.read_feedback(self.f.store.connection,state_dir=self.state,mission=self.f.mission,company_ref=self.f.company['company_ref'],config=self.model.config)
            self.assertEqual(value['status'],'verified');self.assertTrue(value['items'][0]['findings'])
            self.assertFalse(value['items'][0]['retry_authority']);self.assertEqual(len(self.f.adapter.served),before)
        self.sources.start()
    def test_context_self_rehash_cannot_change_paid_prompt_company_source_or_prior(self):
        path=self.q.path/'frozen-context.json';original=path.read_bytes();before=len(self.f.adapter.served)
        for mutate in (lambda v:v['context']['pack']['json'].update(unbound='invented metadata'),
                       lambda v:v['context']['source_bindings'].update(other={'document_sha256':'f'*64}),
                       lambda v:v['context']['company'].update(ticker='FOREIGN')):
            v=json.loads(original);mutate(v);v['scope']['context_hash']=content_hash(v['context']);v['content_hash']=content_hash({k:x for k,x in v.items() if k!='content_hash'});feedback._write(path,v)
            with self.assertRaises(ValueError):self.qualify()
            path.write_bytes(original)
        self.assertEqual(len(self.f.adapter.served),before)
    def test_existing_failed_unknown_or_inflight_verifier_never_buys_new_under_new_budget(self):
        r=self.qualify();before=len(self.f.adapter.served)
        # A fixed slot issue with no formal outcome is conservatively retained.
        with mock.patch('dalton_core.cockpit_model.independent_model_call',side_effect=RuntimeError('transport unknown')):
            with self.assertRaisesRegex(RuntimeError,'unknown'):rec.verifier(r,model=self.model,mission=self.f.mission,state=self.state)
        self.model.config['purpose_call_budgets']['initial_screen_verifier']={'max_cost_usd':20,'timeout_seconds':1800}
        with self.assertRaisesRegex(ValueError,'was issued'):rec.verifier(self.qualify(),model=self.model,mission=self.f.mission,state=self.state)
        self.assertEqual(len(self.f.adapter.served),before)
    def test_formal_or_settlement_tampering_and_inventory_overflow_refuse_zero_new_calls(self):
        from dalton_core.thesis_impact_budget import ThesisImpactBudgetStore
        original=ThesisImpactBudgetStore.admission;before=len(self.f.adapter.served)
        def pending(store,**kw):
            row=copy.deepcopy(original(store,**kw));row['settlement']=None;return row
        with mock.patch.object(ThesisImpactBudgetStore,'admission',pending):
            with self.assertRaisesRegex(ValueError,'unsettled'):self.qualify()
        with mock.patch.object(rec,'MAX_WORKS',0):
            with self.assertRaisesRegex(ValueError,'inventory'):self.qualify()
        self.assertEqual(len(self.f.adapter.served),before)
    def test_real_cli_guard_enters_recheck_without_redrafting_and_publishes_only_after_verifier(self):
        args=SimpleNamespace(dry_run=False,as_of=None,summary_dir=str(self.q.path),timeout_seconds=180)
        before=len(self.f.adapter.served)
        with mock.patch.object(cli,'check_sufficiency',return_value={'drafting_allowed':True,'status':'sufficient'}),mock.patch.object(cli,'build_pack',return_value=self.f.pack),mock.patch.object(cli,'record_pack_build'),mock.patch.object(cli,'_publish_strl_revalidated',return_value={'status':'succeeded','formal_authority_writes':1}) as publish:
            result=cli._run_strl(args,state=self.state,store=self.f.store,mission=self.f.mission,playbook={},entry=self.f.company,summary={'drafted':self.f.company},deliverable_authority=self.f.authority,missions=None,claims={},model=self.model)
        self.assertEqual(result['status'],'succeeded');publish.assert_called_once();self.assertEqual(len(self.f.adapter.served),before+1)
        self.assertEqual(publish.call_args.kwargs['verdict']['verdict'],'pass')
    def test_coordinator_validator_change_wakes_old_hold_once_without_adding_retry_authority(self):
        from dalton_core.initial_screen_launcher import InitialScreenCoordinator
        launcher=mock.Mock();launcher.tickets_dir=self.state/'initial-screens';launcher.state_dir=self.state;launcher.status.return_value={'id':'initial-screen:'+'b'*24,'status':'succeeded','summary':{'status':'held','reason_code':'initial_screen_unchanged_failure'}}
        coordinator=InitialScreenCoordinator(store=self.f.store,launcher=launcher,clock=self.model.clock)
        old=coordinator._signature();feedback._write(launcher.tickets_dir/'latest.json',{'ticket_ref':launcher.status.return_value['id'],'idle_signature':old,'idle_at':self.model.clock().isoformat()})
        launcher.start.return_value={'id':'initial-screen:'+'c'*24,'started_at':self.model.clock().isoformat()}
        self.assertEqual(coordinator.dispatch_once()['status'],'launched');launcher.start.assert_called_once_with()
        self.assertEqual(coordinator.dispatch_once()['status'],'held');self.assertEqual(launcher.start.call_count,1)

    def test_verifier_issue_is_not_reset_by_validator_version_or_alias_ticket_and_quiet_defers_before_issue(self):
        result=self.qualify();before=len(self.f.adapter.served)
        feedback._write(self.q.path/'quiesce-request.json',{})
        with self.assertRaisesRegex(ValueError,'cooperative stop'):
            rec.verifier(result,model=self.model,mission=self.f.mission,state=self.state,summary_dir=self.q.path)
        self.assertFalse((self.state/'initial-screen-verifier-slots').exists())
        (self.q.path/'quiesce-request.json').unlink()
        with mock.patch('dalton_core.cockpit_model.independent_model_call',side_effect=RuntimeError('unknown send')):
            with self.assertRaises(RuntimeError):rec.verifier(result,model=self.model,mission=self.f.mission,state=self.state)
        changed=copy.deepcopy(result);changed['record']['source_ticket_ref']='initial-screen:'+'e'*24
        changed['record']['validator_fingerprint']='f'*64
        with self.assertRaisesRegex(ValueError,'was issued'):rec.verifier(changed,model=self.model,mission=self.f.mission,state=self.state)
        self.assertEqual(len(self.f.adapter.served),before)

    def test_ready_original_verifier_blocks_new_purchase_even_after_budget_change(self):
        from dalton_core.cockpit_model import build_work
        from dalton_core.scheduler import Scheduler
        result=self.qualify();prompt=build_verifier_prompt(result['sections'],worksheet=result['worksheet'],pack=self.f.pack)
        work=build_work(purpose='initial_screen_verifier',request_id=result['record']['verifier_slot'],prompt=prompt,
            mission_version_ref=self.f.mission['id'],max_input_tokens=120000,max_output_tokens=3000,max_cost_usd=1,max_seconds=180,
            created_at=self.f.mission['created_at'])
        with Scheduler(self.model.scheduler_db) as scheduler:scheduler.enqueue(work)
        before=len(self.f.adapter.served)
        self.model.config['purpose_call_budgets']['initial_screen_verifier']={'max_cost_usd':20}
        with self.assertRaisesRegex(ValueError,'terminal formal'):self.qualify()
        self.assertEqual(len(self.f.adapter.served),before)

    def test_natural_coordinator_to_cli_to_original_verifier_has_no_new_draft_or_repair(self):
        from dalton_core.initial_screen_launcher import InitialScreenCoordinator
        args=SimpleNamespace(dry_run=False,as_of=None,summary_dir=str(self.q.path),timeout_seconds=180)
        before=len(self.f.adapter.served);launcher=mock.Mock();launcher.tickets_dir=self.state/'initial-screens';launcher.state_dir=self.state
        launcher.status.return_value={'id':self.q.ticket['id'],'status':'failed','summary':{'status':'failed','drafted':self.f.company}}
        coordinator=InitialScreenCoordinator(store=self.f.store,launcher=launcher,clock=self.model.clock)
        feedback._write(launcher.tickets_dir/'latest.json',{'ticket_ref':self.q.ticket['id'],'idle_signature':coordinator._signature(),'idle_at':self.model.clock().isoformat()})
        results=[]
        def start():
            results.append(cli._run_strl(args,state=self.state,store=self.f.store,mission=self.f.mission,playbook={},entry=self.f.company,
                summary={'drafted':self.f.company},deliverable_authority=self.f.authority,missions=None,claims={},model=self.model))
            return {'id':'initial-screen:'+'e'*24,'started_at':self.model.clock().isoformat()}
        launcher.start.side_effect=start
        with mock.patch.object(cli,'check_sufficiency',return_value={'drafting_allowed':True,'status':'sufficient'}),mock.patch.object(cli,'build_pack',return_value=self.f.pack),mock.patch.object(cli,'record_pack_build'),mock.patch.object(cli,'_publish_strl_revalidated',return_value={'status':'succeeded','formal_authority_writes':1}):
            self.assertEqual(coordinator.dispatch_once()['status'],'launched')
            self.assertEqual(results[0]['status'],'succeeded')
            self.assertEqual(coordinator.dispatch_once()['status'],'held')
        self.assertEqual(len(self.f.adapter.served),before+1)
        self.assertEqual(set(self.qualify()['slots']),{'worksheet','document:0','verify:0'})

    def test_current_run_refuses_before_issue_and_paid_fallback_cannot_admit_twice(self):
        result=self.qualify();before=len(self.f.adapter.served)
        self.model.config['purpose_run_budgets']={'initial_screen':{'max_cost_usd':0.01}}
        with self.assertRaisesRegex(ValueError,'verifier reserve'):
            rec.verifier(result,model=self.model,mission=self.f.mission,state=self.state)
        self.assertFalse(list((self.state/'initial-screen-verifier-slots').glob('*.json')))
        self.assertEqual(len(self.f.adapter.served),before)
        self.model.config['purpose_run_budgets']['initial_screen']['max_cost_usd']=112
        from dalton_core.model_router import ModelRouter
        with ModelRouter(self.f.router_db,read_only=True) as router:
            profiles=router.connection.execute('SELECT profile_json FROM model_endpoint_profile_versions').fetchall()
        self.f.adapter.script={json.loads(row[0])['id']:{'code':'TIMEOUT','message':'paid unknown timeout'} for row in profiles}
        from dalton_core.cockpit_model import CockpitModelError
        with self.assertRaises(CockpitModelError):
            rec.verifier(result,model=self.model,mission=self.f.mission,state=self.state)
        self.assertEqual(len(self.f.adapter.served),before+1)
        self.model.config['purpose_run_budgets']['initial_screen']['max_cost_usd']=224
        with self.assertRaises(ValueError):self.qualify()
        self.assertEqual(len(self.f.adapter.served),before+1)
