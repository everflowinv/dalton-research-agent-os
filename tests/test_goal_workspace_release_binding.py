"""Real isolated first-mission confirmation across closed official release updates."""
import copy,json,os,unittest
from pathlib import Path
from unittest.mock import patch
from dalton_core.store import DaltonStore,content_hash
from dalton_core.workspace import load_workspace_manifest
from dalton_core.workspace_runtime_setup import install
from dalton_core.workspace_mission_setup import plan_first_mission_goal,publish_first_mission_to_store,WorkspaceMissionSetupError
from dalton_core.workspace_goal_binding import record_transition,verify_binding,HISTORY,MAX_HOPS
from tests import test_workspace_runtime_setup as fixtures

class GoalWorkspaceReleaseBindingTests(unittest.TestCase):
    def setUp(self):
        self.f=fixtures.WorkspaceRuntimeSetupTests();self.f.setUp();self.addCleanup(self.f.tearDown)
        install(self.f.manifest,actor_ref='human:owner@example.com',stage_host_lanes=False)
        self.workspace=load_workspace_manifest(self.f.manifest);self.foundation=json.loads((self.f.state/'research-foundation.json').read_bytes());self.calls=0
        class Model:
            def call_setup(model,**kw):
                self.calls+=1
                return {'text':json.dumps({'industry':{'name':'semiconductors'},'suggested_companies':[{'ticker':'ASML'}],'research_questions':['How durable is pricing power?'],'title':'ASML pricing','objective':'Understand pricing power'})}
        self.draft=plan_first_mission_goal(Model(),self.workspace,goal='Research ASML',method_foundation=self.foundation,request_id='one-goal',created_at='2026-10-11T00:00:00Z',company_resolver=lambda t:{'ticker':t,'cik':'937966','name':'Synthetic ASML'})
        self.initial_raw=self.f.manifest.read_bytes();self.counter=0
    def legacy(self):
        self.draft.pop('workspace_identity_binding');self.draft['content_hash']=content_hash({k:v for k,v in self.draft.items()if k!='content_hash'})
    def transition(self,*,write=True,mutate=None,apply=True):
        self.counter+=1;old=self.f.manifest.read_bytes();new=json.loads(old);release=(self.f.root/f'release-{self.counter}'/'venv').resolve();release.mkdir(parents=True);new['release_ref']='release:sha256:'+format(self.counter,'064x');new['release_path']=str(release);new['shared_readonly_paths']=sorted(set(new['shared_readonly_paths'])|{str(release)})
        if mutate:mutate(new)
        new['content_hash']=content_hash({k:v for k,v in new.items()if k!='content_hash'});proof=record_transition(old,new)if write else None
        if apply:self.f.manifest.write_text(json.dumps(new));self.workspace=load_workspace_manifest(self.f.manifest)
        return old,new,Path(proof)if proof else None
    def publish(self,**kwargs):
        with DaltonStore(self.f.state/'core.sqlite')as store:
            return publish_first_mission_to_store(store,self.workspace,proposal=self.draft,proposal_hash=kwargs.get('hash',self.draft['content_hash']),actor_ref=kwargs.get('actor','human:owner@example.com'),method_foundation=self.foundation)
    def test_legacy_saved_draft_publishes_real_authorities_without_new_model_or_draft(self):
        self.legacy();before=copy.deepcopy(self.draft);self.transition();r=self.publish();self.assertEqual(r['version'],1);self.assertEqual(self.draft,before);self.assertEqual(self.calls,1)
        with DaltonStore(self.f.state/'core.sqlite')as store:self.assertEqual(store.connection.execute('SELECT count(*) FROM coverage_mission_pointer').fetchone()[0],1)
    def test_new_versioned_binding_survives_two_release_only_updates(self):
        self.assertEqual(self.draft['workspace_identity_binding']['schema_version'],'workspace-first-goal-stable-binding:0.1');self.transition();self.transition();self.assertEqual(self.publish()['version'],1);self.assertEqual(self.calls,1)
    def test_missing_history_refuses_even_same_uuid_and_scope(self):
        self.legacy();self.transition(write=False)
        with self.assertRaisesRegex(WorkspaceMissionSetupError,'proof is unavailable'):self.publish()
    def test_missing_historical_release_refuses_with_setup_error(self):
        self.legacy();original_release=self.workspace.release_path;self.transition();original_release.rmdir()
        with self.assertRaisesRegex(WorkspaceMissionSetupError,'binding cannot be verified'):self.publish()
    def test_raw_original_bytes_or_receipt_hash_tamper_refuses(self):
        self.legacy();_,_,proof=self.transition();wire=json.loads(proof.read_text());wire['old_manifest_json']+=' ';wire['content_hash']=content_hash({k:v for k,v in wire.items()if k!='content_hash'});proof.write_text(json.dumps(wire));proof.chmod(0o600)
        with self.assertRaisesRegex(WorkspaceMissionSetupError,'prior manifest bytes differ'):self.publish()
    def test_identity_scope_port_lifecycle_changes_cannot_be_recorded_as_release(self):
        for key,value in [('workspace_id','00000000-0000-0000-0000-000000000000'),('cockpit_port',18899),('lifecycle','paused'),('slug','other')]:
            with self.subTest(key=key),self.assertRaisesRegex(ValueError,'stable identity'):self.transition(mutate=lambda m:m.update({key:value}))
    def test_arbitrary_readonly_addition_or_removal_cannot_be_recorded(self):
        for mutate in [lambda m:m['shared_readonly_paths'].append(str(self.f.root/'unapproved-data')),lambda m:m['shared_readonly_paths'].remove(self.workspace.release_path.as_posix())]:
            with self.subTest(),self.assertRaisesRegex(ValueError,'non-release'):self.transition(mutate=mutate)
    def test_stale_scope_confirmation_actor_and_host_settings_gates_remain_strict(self):
        self.transition()
        with self.assertRaisesRegex(WorkspaceMissionSetupError,'confirmation hash differs'):self.publish(hash='0'*64)
        with self.assertRaisesRegex(WorkspaceMissionSetupError,'human:'):self.publish(actor='automation:maintenance')
        with patch('dalton_core.host_research_settings.defaults_for_workspace',return_value=({},'f'*64)),self.assertRaisesRegex(WorkspaceMissionSetupError,'共用预算'):self.publish()
        altered=copy.deepcopy(self.draft);altered['mission_body']['objective']='tampered';original=self.draft;self.draft=altered
        with self.assertRaisesRegex(WorkspaceMissionSetupError,'confirmation hash differs'):self.publish()
        self.draft=original
    def test_proof_prepared_for_failed_switch_is_not_current_authority(self):
        self.legacy();_,future,_=self.transition(apply=False);verify_binding(self.workspace,self.draft)
        forged=copy.deepcopy(self.draft);forged['workspace_manifest_hash']=future['content_hash'];forged['content_hash']=content_hash({k:v for k,v in forged.items()if k!='content_hash'})
        with self.assertRaisesRegex(ValueError,'proof is unavailable'):verify_binding(self.workspace,forged)
    def test_rollback_does_not_use_future_proof_to_confirm_new_release_draft(self):
        self.transition();new_draft=copy.deepcopy(self.draft);new_draft['workspace_manifest_hash']=self.workspace.content_hash;new_draft.pop('workspace_identity_binding');self.f.manifest.write_bytes(self.initial_raw);self.workspace=load_workspace_manifest(self.f.manifest)
        with self.assertRaisesRegex(ValueError,'proof is unavailable'):verify_binding(self.workspace,new_draft)
    def test_proof_private_permissions_symlink_and_exclusive_collision(self):
        old,new,proof=self.transition();self.assertEqual(record_transition(old,new),str(proof));proof.chmod(0o644)
        with self.assertRaisesRegex(ValueError,'permissions'):verify_binding(self.workspace,self.draft)
        proof.chmod(0o600);body=proof.read_bytes();proof.unlink();target=proof.parent/'other';target.write_bytes(body);proof.symlink_to(target)
        with self.assertRaisesRegex(ValueError,'symlink'):verify_binding(self.workspace,self.draft)
        with self.assertRaisesRegex(ValueError,'collision'):record_transition(old,new)
    def test_hop_bound_and_cycle_refuse_without_trying_another_history_branch(self):
        self.transition();self.transition()
        with patch('dalton_core.workspace_goal_binding.MAX_HOPS',1),self.assertRaisesRegex(ValueError,'bounded hops'):verify_binding(self.workspace,self.draft)
        _,new,proof=self.transition();wire=json.loads(proof.read_text());wire['old_manifest_json']=json.dumps(new);wire['old_manifest_file_sha256']=__import__('hashlib').sha256(wire['old_manifest_json'].encode()).hexdigest();wire['old_manifest_hash']=new['content_hash'];wire['content_hash']=content_hash({k:v for k,v in wire.items()if k!='content_hash'});proof.write_text(json.dumps(wire));proof.chmod(0o600)
        with self.assertRaisesRegex(ValueError,'cycle'):verify_binding(self.workspace,self.draft)
    def switch_plan(self):
        import argparse,importlib.util
        self.counter+=1;release=(self.f.root/f'official-{self.counter}'/'venv').resolve();release.mkdir(parents=True)
        manager=self.f.root/'manager.json';manager.write_text(json.dumps({'release_path':str(self.workspace.release_path),'release_ref':self.workspace.release_ref,'shared_readonly_paths':[str(self.workspace.release_path)]}))
        args=argparse.Namespace(backup_dir=self.f.root/'backups',writer_ready_seconds=1,settle_seconds=0,publication_stop_receipt=None)
        plan={'launch_agents':[],'workspaces':[{'path':str(self.f.manifest),'slug':self.workspace.slug}],'release_path':str(release),'release_ref':'release:sha256:'+format(self.counter,'064x'),'manager':{'path':str(manager)},'release_pointer':str(self.f.root/'current-release.json'),'runtime_pointer':str(self.f.root/'current-runtime.json'),'source_commit':'a'*40,'candidate_manifest_sha256':'b'*64,'worker_config':{'path':str(self.f.root/'missing-worker.json')}}
        script=Path(__file__).resolve().parents[1]/'scripts/release_switch.py';spec=importlib.util.spec_from_file_location('isolated_goal_release_switch',script);module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module);return module,args,plan
    def test_actual_official_switch_records_proof_before_manifest_then_original_goal_publishes(self):
        self.legacy();script,args,plan=self.switch_plan()
        with patch.object(script.time,'sleep'):out=script.apply_switch(args,plan)
        self.workspace=load_workspace_manifest(self.f.manifest);proof=self.f.state/HISTORY/(self.workspace.content_hash+'.json');self.assertTrue(proof.is_file());self.assertTrue(any('preserved workspace release transition'in x for x in out['actions']));self.assertEqual(self.publish()['version'],1)
    def test_actual_failed_switch_leaves_only_unreferenced_proof_and_next_target_can_succeed(self):
        self.legacy();script,args,plan=self.switch_plan()
        with patch.object(script,'atomic_write_json',side_effect=OSError('synthetic switch failure')),self.assertRaisesRegex(OSError,'synthetic'):script.apply_switch(args,plan)
        self.assertEqual(self.f.manifest.read_bytes(),self.initial_raw);verify_binding(self.workspace,self.draft)
        abandoned=list((self.f.state/HISTORY).glob('*.json'));self.assertEqual(len(abandoned),1)
        script,args,plan=self.switch_plan()
        with patch.object(script.time,'sleep'):script.apply_switch(args,plan)
        self.workspace=load_workspace_manifest(self.f.manifest);self.assertEqual(len(list((self.f.state/HISTORY).glob('*.json'))),2);self.assertEqual(self.publish()['version'],1)
