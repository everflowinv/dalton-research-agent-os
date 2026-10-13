"""Offline paid-formal boundary and replay tests; adapters are synthetic."""
import copy
import hashlib
import json
import os
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest import mock
from tests import test_initial_screen_failure_feedback as fixtures
from dalton_core import initial_screen_quiesce as quiesce
from dalton_core import initial_screen_failure_feedback as feedback
from dalton_core import initial_screen_cli as cli
from dalton_core.initial_screen_launcher import InitialScreenLauncher, InitialScreenCoordinator, InitialScreenLaunchRejected
from dalton_core.store import content_hash


class InitialScreenQuiesceTests(unittest.TestCase):
    def setUp(self):
        self.fixture = fixtures.InitialScreenFailureFeedbackTests('runTest')
        from tests import p9a_fixtures
        original = p9a_fixtures.mission_params
        def params(*a, **kw):
            value=original(*a, **kw)
            value['autonomy']['may_write'].append('deliverable')
            return value
        with mock.patch.object(p9a_fixtures,'mission_params',side_effect=params):self.fixture.setUp()
        self.addCleanup(self.fixture.doCleanups)
        f = self.fixture
        self.root, self.model, self.adapter = f.root, f.model, f.adapter
        self.path = f.directory
        self.config_path = (self.root / 'model.json').resolve()
        self.config_path.write_text(json.dumps(self.model.config))
        self.ticket = self.new_ticket(self.path)
        self.context = {'mode':'strl', 'contract':cli.STRL_CONTRACT_VERSION,
            'company':f.company,'pack':f.pack,'prior_reference':None,'source_bindings':{},
            'entry':f.company,'playbook':{}}

    def new_ticket(self, directory, **extra):
        directory.mkdir(mode=0o700,exist_ok=True)
        record={'schema_version':'0.1','id':'initial-screen:'+directory.name,
            'pid':os.getpid(),'status':'running','exit_code':None,'completed_at':None,
            'started_at':'2026-10-11T17:00:00+00:00','command':['python','-m','dalton_core.initial_screen_cli'],
            'model_config_path':str(self.config_path),
            'model_config_fingerprint':hashlib.sha256(self.config_path.read_bytes()).hexdigest(),**extra}
        feedback._write(directory/'ticket.json',record)
        return record

    def proxy(self, directory=None, context=None, resume=None):
        f=self.fixture
        return quiesce.RunQuiesce(self.model,directory or self.path,context=context or self.context,
            mission=f.mission,authority=f.authority,resume_directory=resume)

    def request(self, directory=None):
        path=directory or self.path
        with mock.patch('dalton_core.lane_child_launcher.ticket_identity',return_value=True):
            return quiesce.request_quiesce(path,expected_ticket_hash=content_hash(feedback._read(path/'ticket.json')))

    def call(self, proxy, name='worksheet', prompt='Frozen synthetic worksheet', purpose='initial_screen', **kw):
        return proxy.call(purpose=purpose,request_id=name,prompt=prompt,mission=self.fixture.mission,**kw)

    def pause(self):
        proxy=self.proxy(); answer=self.call(proxy)
        self.request()
        with self.assertRaises(quiesce.ScreenQuiesced) as caught:self.call(proxy,name='document')
        summary=quiesce.paused_summary({'drafted':self.fixture.company},caught.exception)
        feedback._write(self.path/'summary.json',summary)
        ticket={**self.ticket,'status':'quiesced','exit_code':0,'completed_at':'2026-10-11T17:01:00+00:00'}
        feedback._write(self.path/'ticket.json',ticket)
        return answer,summary

    def test_current_paid_call_finishes_then_next_step_pauses_and_replays_for_free(self):
        proxy=self.proxy(); original=self.adapter.execute
        def execute(*args):
            result=original(*args);self.request();return result
        with mock.patch.object(self.adapter,'execute',side_effect=execute):
            first=self.call(proxy)
        before=len(self.adapter.served)
        with self.assertRaises(quiesce.ScreenQuiesced) as caught:self.call(proxy,name='document')
        proof=caught.exception.checkpoint
        self.assertEqual(proof['completed'][0]['work_order_ref'],first['work_order_ref'])
        self.assertGreater(proof['completed'][0]['accounted_micros'],0)
        self.assertEqual(len(self.adapter.served),before)
        summary=quiesce.paused_summary({},caught.exception)
        self.assertEqual(summary['status'],'quiesced');self.assertIsNone(summary['gate'])
        self.assertEqual(summary['formal_authority_writes'],0)
        feedback._write(self.path/'summary.json',summary)
        feedback._write(self.path/'ticket.json',{**self.ticket,'status':'quiesced','exit_code':0,'completed_at':'2026-10-11T17:01:00+00:00'})
        successor=self.path.with_name('b'*24);self.new_ticket(successor,resumed_from=self.ticket['id'])
        resumed=self.proxy(directory=successor,resume=self.path)
        replay=self.call(resumed)
        self.assertEqual(replay['work_order_ref'],first['work_order_ref']);self.assertTrue(replay['replayed'])
        self.assertEqual(replay['cost_micros'],0);self.assertEqual(len(self.adapter.served),before)
        self.call(resumed,name='document')
        self.assertEqual(len(self.adapter.served),before+1)
        # Immutable parent pause is retained, not cleared to buy another call.
        self.assertTrue((self.path/'quiesce-request.json').exists())
        self.assertEqual(feedback._read(self.path/'step-checkpoint.json'),proof)

    def test_pause_before_first_call_spends_nothing(self):
        context=copy.deepcopy(self.context);context['pack']['tags']={}
        from dalton_core.material_pack import _sha
        pack=context['pack'];pack['content_hash']=_sha({'recipe':pack['recipe_version'],'text':pack['text'],'tags':pack['tags']})
        proxy=self.proxy(context=context);self.request();before=len(self.adapter.served)
        with self.assertRaises(quiesce.ScreenQuiesced) as caught:self.call(proxy)
        self.assertEqual(caught.exception.checkpoint['completed'],[])
        self.assertEqual(len(self.adapter.served),before)

    def test_unsettled_or_wrong_mission_or_unknown_call_cannot_resume(self):
        from dalton_core.thesis_impact_budget import ThesisImpactBudgetStore
        original=ThesisImpactBudgetStore.admission
        for kind in ('pending','foreign'):
            with self.subTest(kind=kind):
                proxy=self.proxy();self.call(proxy,name=kind)
                self.request()
                def admission(instance,**kwargs):
                    value=copy.deepcopy(original(instance,**kwargs))
                    if kind=='pending':value['settlement']=None
                    else:value['mission_binding']={'mission_version_ref':'other','mission_version_hash':'0'*64}
                    return value
                before=len(self.adapter.served)
                with mock.patch.object(ThesisImpactBudgetStore,'admission',admission):
                    with self.assertRaises(quiesce.ScreenQuiesceUnproved):self.call(proxy,name=kind+'next')
                self.assertEqual(len(self.adapter.served),before)
                (self.path/'quiesce-request.json').unlink()
        proxy=self.proxy();self.call(proxy,name='unknown');proxy.records[0]['call']['cost_status']='unknown';self.request()
        with self.assertRaises(quiesce.ScreenQuiesceUnproved):self.call(proxy,name='unknown-next')

    def test_rehashed_checkpoint_formal_or_scope_changes_cannot_authorize_call(self):
        _,summary=self.pause();saved=feedback._read(self.path/'step-checkpoint.json')
        successor=self.path.with_name('b'*24);self.new_ticket(successor,resumed_from=self.ticket['id'])
        before=len(self.adapter.served)
        for change in (lambda v:v['calls'][0]['call'].update(result_envelope_ref='result:false'),
                       lambda v:v['scope'].update(mission_hash='0'*64),
                       lambda v:v['context']['pack'].update(text='changed original')):
            value=copy.deepcopy(saved);change(value);value['content_hash']=content_hash({k:v for k,v in value.items()if k!='content_hash'})
            feedback._write(self.path/'step-checkpoint.json',value)
            feedback._write(self.path/'summary.json',{**summary,'checkpoint_hash':value['content_hash']})
            with self.assertRaises((*feedback.PROOF_ERRORS,quiesce.ScreenQuiesceUnproved)):
                self.proxy(directory=successor,resume=self.path)
        self.assertEqual(len(self.adapter.served),before)

    def test_changed_actual_prompt_or_budget_refuses_before_new_call(self):
        _,_=self.pause();successor=self.path.with_name('b'*24);self.new_ticket(successor,resumed_from=self.ticket['id'])
        proxy=self.proxy(directory=successor,resume=self.path);before=len(self.adapter.served)
        with self.assertRaises(quiesce.ScreenQuiesceUnproved):self.call(proxy,prompt='different source or repair')
        self.model.config['purpose_call_budgets']['initial_screen']['max_output_tokens']+=1
        with self.assertRaises((*feedback.PROOF_ERRORS,quiesce.ScreenQuiesceUnproved)):
            self.proxy(directory=successor,resume=self.path)
        self.assertEqual(len(self.adapter.served),before)

    def test_legacy_no_ticket_remains_original_model(self):
        args=SimpleNamespace(summary_dir=self.root/'no-ticket')
        self.assertIs(quiesce.wrap(self.model,args,context=self.context,mission=self.fixture.mission,authority=self.fixture.authority),self.model)

    def test_cli_pause_does_not_buy_worksheet_repair_or_fake_final_failure(self):
        # Real CockpitModel and stored paid formal; the first invalid response
        # is deterministically checked before the existing repair is paused.
        args=SimpleNamespace(dry_run=False,as_of=None,summary_dir=str(self.path),timeout_seconds=180)
        proxy_request=self.adapter.execute
        def execute(*a):
            value=proxy_request(*a);self.request();return value
        before=len(self.adapter.served)
        with mock.patch.object(cli,'check_sufficiency',return_value={'drafting_allowed':True,'status':'sufficient'}), \
             mock.patch.object(cli,'build_pack',return_value=self.fixture.pack),mock.patch.object(cli,'record_pack_build'), \
             mock.patch.object(self.adapter,'execute',side_effect=execute):
            with self.assertRaises(quiesce.ScreenQuiesced) as caught:
                cli._run_strl(args,state=self.root,store=self.fixture.store,mission=self.fixture.mission,
                    playbook={},entry=self.fixture.company,summary={'created_at':'2026-10-11T17:00:00+00:00'},
                    deliverable_authority=self.fixture.authority,missions=None,claims={},model=self.model)
        summary=quiesce.paused_summary({},caught.exception)
        self.assertEqual(len(self.adapter.served),before+1)
        self.assertNotIn('validation_feedback',summary)
        self.assertEqual(summary['status'],'quiesced')

    def test_cli_resume_reuses_paid_invalid_worksheet_and_only_original_one_repair(self):
        args=SimpleNamespace(dry_run=False,as_of=None,summary_dir=str(self.path),timeout_seconds=180)
        original=self.adapter.execute
        def execute(*a):
            value=original(*a);self.request();return value
        before=len(self.adapter.served)
        with mock.patch.object(cli,'check_sufficiency',return_value={'drafting_allowed':True,'status':'sufficient'}), \
             mock.patch.object(cli,'build_pack',return_value=self.fixture.pack),mock.patch.object(cli,'record_pack_build'), \
             mock.patch.object(self.adapter,'execute',side_effect=execute):
            with self.assertRaises(quiesce.ScreenQuiesced) as caught:
                cli._run_strl(args,state=self.root,store=self.fixture.store,mission=self.fixture.mission,
                    playbook={},entry=self.fixture.company,summary={},deliverable_authority=self.fixture.authority,
                    missions=None,claims={},model=self.model)
        pause=quiesce.paused_summary({},caught.exception)
        feedback._write(self.path/'summary.json',pause)
        feedback._write(self.path/'ticket.json',{**self.ticket,'status':'quiesced','exit_code':0,'completed_at':'2026-10-11T17:01:00+00:00'})
        successor=self.path.with_name('b'*24);self.new_ticket(successor,resumed_from=self.ticket['id'])
        args.summary_dir=str(successor)
        def last_repair(*a):
            value=original(*a);self.request(successor);return value
        with mock.patch.object(cli,'check_sufficiency') as suff, mock.patch.object(cli,'build_pack') as build, \
             mock.patch.object(self.adapter,'execute',side_effect=last_repair):
            result=cli._run_strl(args,state=self.root,store=self.fixture.store,mission=self.fixture.mission,
                playbook={},entry=self.fixture.company,summary={'drafted':self.fixture.company},deliverable_authority=self.fixture.authority,
                missions=None,claims={},model=self.model)
            suff.assert_not_called();build.assert_not_called()
        self.assertEqual(result['status'],'failed')
        self.assertEqual(result['validation_feedback']['status'],'recorded')
        self.assertEqual(len(self.adapter.served),before+2) # first response + its sole original repair
        checkpoint=feedback._read(self.path/'step-checkpoint.json')
        self.assertEqual(checkpoint['calls'][0]['call']['work_order_ref'],pause['completed_steps'][0]['work_order_ref'])
        feedback._write(successor/'summary.json',result)
        feedback._write(successor/'ticket.json',{**feedback._read(successor/'ticket.json'),'status':'failed','exit_code':1,'completed_at':'2026-10-11T17:02:00+00:00'})
        self.assertEqual(self.fixture.read()['status'],'verified')

    def test_actual_verifier_independence_survives_pause_and_free_replay(self):
        from dalton_core import host_research_reconcile
        original=host_research_reconcile.resolved_model_config
        def resolve(state, config, purpose):
            resolved=dict(original(state,config,purpose))
            if purpose=='initial_screen_verifier':
                resolved['routing_policy_ref']=self.fixture.verifier_policy
                resolved['credential_slot_refs']=self.fixture.verifier_slots
            return resolved
        patch=mock.patch.object(host_research_reconcile,'resolved_model_config',side_effect=resolve)
        patch.start();self.addCleanup(patch.stop)
        proxy=self.proxy();first=self.call(proxy)
        answer=self.call(proxy,name='verifier',purpose='initial_screen_verifier',
            producer_route_decision_refs=[first['route_decision_ref']])
        self.assertNotEqual(answer['route_decision_ref'],first['route_decision_ref'])
        self.request()
        with self.assertRaises(quiesce.ScreenQuiesced) as caught:self.call(proxy,name='document-repair')
        feedback._write(self.path/'summary.json',quiesce.paused_summary({},caught.exception))
        feedback._write(self.path/'ticket.json',{**self.ticket,'status':'quiesced','exit_code':0,'completed_at':'2026-10-11T17:01:00+00:00'})
        successor=self.path.with_name('b'*24);self.new_ticket(successor,resumed_from=self.ticket['id'])
        resumed=self.proxy(directory=successor,resume=self.path);before=len(self.adapter.served)
        self.call(resumed)
        replay=self.call(resumed,name='verifier',purpose='initial_screen_verifier',
            producer_route_decision_refs=[first['route_decision_ref']])
        self.assertEqual(replay['work_order_ref'],answer['work_order_ref']);self.assertEqual(len(self.adapter.served),before)

    def test_original_passage_changed_or_missing_refuses_without_another_call(self):
        from dalton_core.product_contract import tag_entry
        held={tag_entry(self.fixture.pack,t)['ref']:tag_entry(self.fixture.pack,t)['text'] for t in self.fixture.pack['tags'] if t.startswith('P')}
        self.context['source_bindings']={ref:{'document_ref':ref,'document_sha256':content_hash(text),'start':0,'end':len(text)} for ref,text in held.items()}
        originals={ref:{**self.context['source_bindings'][ref],'passage_hash':content_hash(text),'text':text} for ref,text in held.items()}
        self.adapter.value={'key_passages':['P1']}
        with mock.patch.object(feedback,'_checked_originals',return_value=originals):
            first,summary=self.pause()
        successor=self.path.with_name('b'*24);self.new_ticket(successor,resumed_from=self.ticket['id'])
        changed=copy.deepcopy(originals)
        ref=tag_entry(self.fixture.pack,'P1')['ref'];changed[ref]['document_sha256']='0'*64
        before=len(self.adapter.served)
        with mock.patch.object(feedback,'_checked_originals',return_value=changed):
            with self.assertRaises((*feedback.PROOF_ERRORS,quiesce.ScreenQuiesceUnproved)):
                self.proxy(directory=successor,resume=self.path)
        self.assertEqual(len(self.adapter.served),before)

    def test_legacy_draft_scope_also_strongly_replays_the_same_paid_step(self):
        context={'mode':'legacy','company':self.fixture.company,'claim_refs':[],
            'prior_reference':None,'entry':self.fixture.company,'playbook':{}}
        proxy=self.proxy(context=context);answer=self.call(proxy,purpose='draft')
        self.request()
        with self.assertRaises(quiesce.ScreenQuiesced) as caught:self.call(proxy,name='draft-repair',purpose='draft')
        feedback._write(self.path/'summary.json',quiesce.paused_summary({},caught.exception))
        feedback._write(self.path/'ticket.json',{**self.ticket,'status':'quiesced','exit_code':0,'completed_at':'2026-10-11T17:01:00+00:00'})
        successor=self.path.with_name('b'*24);self.new_ticket(successor,resumed_from=self.ticket['id'])
        resumed=self.proxy(directory=successor,context=context,resume=self.path);before=len(self.adapter.served)
        replay=self.call(resumed,purpose='draft')
        self.assertEqual(replay['work_order_ref'],answer['work_order_ref']);self.assertEqual(len(self.adapter.served),before)

    def test_real_child_live_identity_requests_pause_cli_exits_zero_without_completion(self):
        import subprocess,sys
        from datetime import datetime,timezone
        script=self.root/'offline-screen-child.py'
        script.write_text("""
import hashlib,json,os,sys
from pathlib import Path
from unittest.mock import patch
from tests import test_initial_screen_failure_feedback as fixtures,p9a_fixtures
from dalton_core import initial_screen_cli as cli,initial_screen_quiesce as q,initial_screen_failure_feedback as feedback
from dalton_core.store import content_hash
original=p9a_fixtures.mission_params
def params(*a,**kw):
    value=original(*a,**kw);value['autonomy']['may_write'].append('deliverable');return value
f=fixtures.InitialScreenFailureFeedbackTests('runTest')
with patch.object(p9a_fixtures,'mission_params',side_effect=params):f.setUp()
try:
    directory=f.directory;config=(f.root/'model.json').resolve();config.write_text(json.dumps(f.model.config))
    ticket={'schema_version':'0.1','id':'initial-screen:'+directory.name,'pid':os.getpid(),
        'status':'running','exit_code':None,'completed_at':None,'started_at':sys.argv[2],
        'command':[sys.executable,*sys.argv],'model_config_path':str(config),
        'model_config_fingerprint':hashlib.sha256(config.read_bytes()).hexdigest()}
    feedback._write(directory/'ticket.json',ticket)
    execute=f.adapter.execute
    def paid(*a):
        result=execute(*a)
        q.request_quiesce(directory,expected_ticket_hash=content_hash(ticket))
        return result
    with patch.object(cli,'CockpitModel',return_value=f.model),patch.object(cli,'_playbook',return_value={}), \
         patch.object(cli,'evaluate_mission',return_value=[]),patch.object(cli,'_target',return_value=(f.company,[])), \
         patch.object(cli,'check_sufficiency',return_value={'drafting_allowed':True,'status':'sufficient'}), \
         patch.object(cli,'build_pack',return_value=f.pack),patch.object(cli,'record_pack_build'), \
         patch.object(f.adapter,'execute',side_effect=paid):
        code=cli.main(['--state-dir',str(f.root),'--model-config',str(config),'--summary-dir',str(directory),'--quiet'])
    summary=feedback._read(directory/'summary.json');checkpoint=feedback._read(directory/'step-checkpoint.json')
    Path(sys.argv[1]).write_text(json.dumps({'exit_code':code,'summary_status':summary['status'],
        'gate':summary['gate'],'formal_authority_writes':summary['formal_authority_writes'],
        'calls':len(f.adapter.served),'checkpoint_calls':len(checkpoint['calls']),
        'settlements':checkpoint['completed'][0]['settlements'],
        'accounted_micros':checkpoint['completed'][0]['accounted_micros'],
        'failure_receipt_exists':(directory/'validation-feedback.json').exists()}))
finally:f.doCleanups()
""")
        result=self.root/'offline-screen-result.json'
        completed=subprocess.run([sys.executable,str(script),str(result),datetime.now(timezone.utc).isoformat()],
            stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True,timeout=20,
            env={**os.environ,'PYTHONPATH':os.pathsep.join([str(Path(__file__).resolve().parents[1]),str(Path(__file__).resolve().parents[1]/'src')])})
        self.assertEqual(completed.returncode,0,completed.stderr[-2000:])
        record=json.loads(result.read_text())
        self.assertEqual(record['exit_code'],0);self.assertEqual(record['summary_status'],'quiesced')
        self.assertIsNone(record['gate']);self.assertEqual(record['formal_authority_writes'],0)
        self.assertEqual(record['checkpoint_calls'],1);self.assertEqual(record['calls'],2) # setup + one paid screen step
        self.assertGreater(record['accounted_micros'],0);self.assertTrue(record['settlements'])
        self.assertFalse(record['failure_receipt_exists'])

    def test_successor_launcher_idempotence_and_coordinator_resume_without_idle_hold(self):
        _,summary=self.pause()
        launcher=InitialScreenLauncher(state_dir=self.root,model_config_path=self.config_path)
        process=mock.Mock(pid=98765);process.poll.return_value=None
        with mock.patch('dalton_core.initial_screen_launcher.subprocess.Popen',return_value=process) as spawn:
            one=launcher.start(resumed_from=self.ticket['id'])
            # A concurrent tick observes the same live successor, never spawns twice.
            with self.assertRaises(Exception):launcher.start(resumed_from=self.ticket['id'])
            self.assertEqual(spawn.call_count,1)
            self.assertEqual(one['resumed_from'],self.ticket['id'])
        launcher._current=None
        # Saved terminal successor stays consumed; no paid restart.
        feedback._write(launcher._ticket_path(one['id']),{**one,'status':'failed','exit_code':1})
        with mock.patch('dalton_core.initial_screen_launcher.subprocess.Popen') as spawn:
            self.assertEqual(launcher.start(resumed_from=self.ticket['id'])['status'],'failed');spawn.assert_not_called()
        coordinator=InitialScreenCoordinator(store=self.fixture.store,launcher=launcher)
        signature=coordinator._signature()
        feedback._write(coordinator._latest_path(),{'ticket_ref':self.ticket['id'],'idle_signature':signature,'idle_at':coordinator.clock().isoformat()})
        with mock.patch.object(launcher,'start',return_value=one) as start:
            result=coordinator.dispatch_once()
        self.assertEqual(result['status'],'launched');start.assert_called_once_with(resumed_from=self.ticket['id'])

    def test_launcher_close_requests_boundary_and_never_terminates_paid_child(self):
        launcher=InitialScreenLauncher(state_dir=self.root,model_config_path=self.config_path)
        process=mock.Mock();process.poll.return_value=None
        launcher._current=(self.ticket['id'],process)
        with mock.patch.object(quiesce,'request_quiesce') as request:
            launcher.close()
        request.assert_called_once_with(self.path.resolve(),expected_ticket_hash=content_hash(self.ticket))
        process.terminate.assert_not_called();process.kill.assert_not_called()

    def test_config_change_or_unproved_pause_launcher_refuses_without_spawn(self):
        self.pause();launcher=InitialScreenLauncher(state_dir=self.root,model_config_path=self.config_path)
        self.config_path.write_text('{}')
        with mock.patch('dalton_core.initial_screen_launcher.subprocess.Popen') as spawn:
            with self.assertRaises(InitialScreenLaunchRejected):launcher.start(resumed_from=self.ticket['id'])
            spawn.assert_not_called()
