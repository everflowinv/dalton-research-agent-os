"""A standard release switch resumes exact cooperative requests before bootstrap."""
import json
import os
from pathlib import Path
import plistlib
import subprocess
import unittest
from unittest.mock import patch
from tests import test_release_tooling as tooling
from dalton_core.research_publication_quiesce import QuiesceControl

switch=tooling.release_switch


class PublicationSwitchResumeTests(unittest.TestCase):
    def setUp(self):
        f=tooling.SwitchPlanTests('test_both_launch_agent_naming_families_are_discovered');f.setUp()
        self.addCleanup(f.doCleanups);self.f=f;self.args=f.args(tooling.RELEASE_B)
        self.args.backup_dir=f.root/'backups';self.args.settle_seconds=0
        self.work=f.root/'state/publication-work';self.work.mkdir(parents=True,mode=0o700)
        self.lock=self.work/'.worker.lock';self.lock.touch(mode=0o600)
        cfg=json.loads(f.worker_config.read_text());cfg['work_dir']=str(self.work);cfg['core_db']=str(self.work.parent/'core.sqlite');f.worker_config.write_text(json.dumps(cfg))
        self.label='com.dalton.research-publication-worker';p=f.agents/(self.label+'.plist')
        wire=plistlib.loads(p.read_bytes());wire['ProgramArguments']=[f.old_venv+'/bin/python','-m','dalton_core.research_output_preparation','run-worker','--config',str(f.worker_config)]
        p.write_bytes(plistlib.dumps(wire))
        request=QuiesceControl(f.worker_config,self.work).request();self.request=request
        info=self.lock.stat();self.receipt=f.root/'stop.json'
        self.proof={'schema_version':'dalton-publication-worker-quiesce-1','apply':True,
            'status':'workers_quiesced_unloaded_after_whole_pass',
            'workers':[{'label':self.label,'config':str(f.worker_config),'lock':str(self.lock),
                        'lock_identity':[info.st_dev,info.st_ino], 'whole_pass_completed_before_bootout':True,
                        'unloaded_under_lock':True,'cooperative_request':request}]}
        self.write_proof();self.args.publication_stop_receipt=self.receipt
        self.commands=[];self.resume_ok=True;self.loaded=False
        readiness=patch.object(switch,"writer_socket_readable",return_value=True)
        readiness.start();self.addCleanup(readiness.stop)

    def write_proof(self):
        self.receipt.write_text(json.dumps(self.proof));self.receipt.chmod(0o600)

    def fake_subprocess(self,args,**kwargs):
        if args[:2]==['launchctl','print']:
            return subprocess.CompletedProcess(args,0 if self.loaded else 1,'','')
        self.assertIn('dalton_core.research_publication_quiesce',args)
        self.assertEqual(args[0],str(Path(self.f.new_venv)/'bin/python'))
        self.assertNotIn('PYTHONPATH',kwargs['env']);self.assertNotIn('PYTHONHOME',kwargs['env'])
        self.assertEqual(args[-1],self.request['content_hash']);self.commands.append('resume')
        if self.resume_ok:return subprocess.CompletedProcess(args,0,json.dumps({'status':'resumed','request_hash':args[-1]}),'')
        return subprocess.CompletedProcess(args,1,'','CAS refused')

    def fake_launch(self,action,label,path):
        self.commands.append(action+':'+label)
        return 'launchctl '+action+' -> 0'

    def plan(self):
        with patch.object(switch.subprocess,'run',side_effect=self.fake_subprocess):
            return switch.build_plan(self.args)

    def test_actual_switch_repoints_and_resumes_before_every_bootstrap(self):
        plan=self.plan()
        with patch.object(switch.subprocess,'run',side_effect=self.fake_subprocess),patch.object(switch,'launchctl',side_effect=self.fake_launch),patch.object(switch.time,'sleep'):
            result=switch.apply_switch(self.args,plan)
        self.assertEqual(result['publication_resumes'][0]['request_hash'],self.request['content_hash'])
        resume=self.commands.index('resume')
        self.assertTrue(all(c.startswith('stop:') for c in self.commands[:resume]))
        self.assertTrue(all(c.startswith('start:') for c in self.commands[resume+1:]))
        argv=plistlib.loads((self.f.agents/(self.label+'.plist')).read_bytes())['ProgramArguments']
        self.assertEqual(argv[0],self.f.new_venv+'/bin/python')
        self.assertEqual(json.loads(self.f.worker_config.read_text())['publication_gate']['expected_release_ref'],plan['release_ref'])

    def test_resume_refusal_prevents_all_bootstrap_and_keeps_request(self):
        plan=self.plan();self.resume_ok=False
        with patch.object(switch.subprocess,'run',side_effect=self.fake_subprocess),patch.object(switch,'launchctl',side_effect=self.fake_launch),patch.object(switch.time,'sleep'):
            with self.assertRaisesRegex(switch.SwitchError,'no labels bootstrapped'):switch.apply_switch(self.args,plan)
        self.assertFalse(any(c.startswith('start:') for c in self.commands))
        self.assertTrue((self.work/'quiesce-request.json').exists())

    def test_changed_request_is_refused_before_any_switch_write(self):
        plan=self.plan();before=self.f.manager.read_bytes()
        changed=json.loads((self.work/'quiesce-request.json').read_text());changed['request_id']='c'*32
        (self.work/'quiesce-request.json').write_text(json.dumps(changed))
        with patch.object(switch.subprocess,'run',side_effect=self.fake_subprocess):
            with self.assertRaises(switch.SwitchError):switch.apply_switch(self.args,plan)
        self.assertEqual(self.f.manager.read_bytes(),before);self.assertFalse(self.args.backup_dir.exists())

    def test_loaded_worker_wrong_lock_and_omitted_scope_refuse_preview(self):
        self.loaded=True
        with self.assertRaisesRegex(switch.SwitchError,'unloaded'):self.plan()
        self.loaded=False;self.proof['workers'][0]['lock_identity']=[1,2];self.write_proof()
        with self.assertRaisesRegex(switch.SwitchError,'lock/config'):self.plan()
        self.proof['workers']=[];self.write_proof()
        with self.assertRaisesRegex(switch.SwitchError,'omits'):self.plan()

    def test_no_receipt_keeps_existing_switch_api_and_plan_shape(self):
        self.args.publication_stop_receipt=None
        plan=switch.build_plan(self.args)
        self.assertNotIn('publication_resumes',plan)
        with patch.object(switch.subprocess,'run',side_effect=AssertionError('resume subprocess called')),patch.object(switch,'launchctl',side_effect=self.fake_launch),patch.object(switch.time,'sleep'):
            switch.apply_switch(self.args,plan)
        self.assertNotIn('resume',self.commands)

    def test_omitted_request_cannot_masquerade_as_legacy_stop_proof(self):
        self.proof['workers'][0].pop('cooperative_request');self.write_proof()
        with self.assertRaisesRegex(switch.SwitchError,'omits a current'):self.plan()

    def test_legacy_whole_pass_receipt_has_no_resume_call(self):
        self.proof['workers'][0].pop('cooperative_request');self.write_proof()
        # Old installation has never published a cooperative request.
        (self.work/'quiesce-request.json').unlink()
        plan=self.plan();self.assertEqual(plan['publication_resumes'],[])
        with patch.object(switch.subprocess,'run',side_effect=AssertionError('unexpected resume')),patch.object(switch,'launchctl',side_effect=self.fake_launch),patch.object(switch.time,'sleep'):
            switch.apply_switch(self.args,plan)
        self.assertNotIn('resume',self.commands)

if __name__=='__main__':unittest.main()
