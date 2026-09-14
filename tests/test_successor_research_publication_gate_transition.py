from __future__ import annotations
import json, os, tempfile, unittest
from pathlib import Path
from types import SimpleNamespace

from scripts.successor_research_publication_gate_transition import (
    ResearchPublicationGateTransitionError, apply, build_transition, expected_state, rollback,
    verify_preserved_publication_state,
)
from scripts.prepare_successor_config_transition import (
    RESEARCH_PUBLICATION_GATE_SCHEMA_VERSION, apply_transition_to_scratch,
    build_preserve_existing_transition, expected_transition_state,
)
from tests.test_successor_config_transition import PreserveExistingTransitionTests
from scripts.run_successor_copied_state_rehearsal import derive_confined_transition
from scripts.execute_successor_stopped_window_candidate import (
    SuccessorExecuteError, validate_publication_gate_rehearsal,
)

def write(path:Path,value:dict)->None:
    path.write_text(json.dumps(value,indent=2)+"\n");os.chmod(path,0o600)
def config(release:str,commit:str)->dict:
    return {"schema_version":"research-publication-worker-config:0.1",
      "core_db":"/state/core.sqlite","scheduler_db":"/state/scheduler.sqlite",
      "model_config":"/state/a","verifier_config":"/state/b","checker_config":"/state/c","brain_config":"/state/d",
      "work_dir":"/state/research-publication-work","output_directory":"/state/research-localization",
      "workers":4,"chunk_chars":4500,"max_cost_per_call":1.0,"draft_attempts":2,
      "publication_gate":{"release_pointer":"/Users/everflow/Projects/dalton-owner-activation-20260910/current-release.json","runtime_pointer":"/Users/everflow/Projects/dalton-owner-activation-20260910/current-runtime-config.json",
        "expected_release_ref":release,"expected_source_commit":commit}}

class GateTransitionTests(unittest.TestCase):
    def setUp(self):
        t=tempfile.TemporaryDirectory();self.addCleanup(t.cleanup);self.root=Path(t.name);self.packet=self.root/'packet';self.packet.mkdir();self.state=self.root/'state';self.state.mkdir();self.launch=self.root/'LaunchAgents';self.launch.mkdir()
        (self.state/'research-localization').mkdir();write(self.state/'research-localization/index.json',{'schema_version':'x'})
        (self.launch/'com.dalton.research-publication-worker.plist').write_bytes(b'plist');os.chmod(self.launch/'com.dalton.research-publication-worker.plist',0o600)
        self.before=self.packet/'before.json';self.after=self.packet/'after.json'
        write(self.before,config('foundation-r25','a'*40));write(self.after,config('foundation-r25b','b'*40))
        self.transition=build_transition(packet_root=self.packet,before_path=self.before,after_path=self.after,state_dir=self.state,launch_agents_dir=self.launch)
        (self.state/'research-publication-worker-config.json').write_bytes(self.before.read_bytes());os.chmod(self.state/'research-publication-worker-config.json',0o600)
    def test_apply_and_idempotent_rollback_preserve_every_other_value(self):
        proof=apply(packet_root=self.packet,state_dir=self.state,transition=self.transition)
        self.assertEqual(self.after.read_bytes(),(self.state/'research-publication-worker-config.json').read_bytes())
        self.assertEqual(1,proof['configuration_mutations'])
        self.assertEqual('rolled_back',rollback(packet_root=self.packet,state_dir=self.state,transition=self.transition)['status'])
        self.assertEqual(0o600,(self.state/'research-publication-worker-config.json').stat().st_mode&0o777)
        self.assertEqual('already_rolled_back',rollback(packet_root=self.packet,state_dir=self.state,transition=self.transition)['status'])

    def test_packet_preflight_gate_branch_closes_exact_rehearsal_proof(self):
        import hashlib
        before, after = expected_state(self.packet, self.transition)
        transition = {
            "research_publication_gate_transition": self.transition}
        proof = {
            "before_sha256": hashlib.sha256(before).hexdigest(),
            "after_sha256": hashlib.sha256(after).hexdigest(),
            "successor": self.transition["successor"],
        }
        validate_publication_gate_rehearsal(
            self.packet, transition,
            {"results": {"research_publication_gate_transition": proof}})
        proof["after_sha256"] = "0" * 64
        with self.assertRaisesRegex(
                SuccessorExecuteError, "does not prove publication gate"):
            validate_publication_gate_rehearsal(
                self.packet, transition,
                {"results": {"research_publication_gate_transition": proof}})
    def test_rejects_non_gate_change_and_cas_drift(self):
        changed=config('foundation-r25b','b'*40);changed['workers']=8;write(self.after,changed)
        with self.assertRaisesRegex(ResearchPublicationGateTransitionError,'only publication gate'):
            build_transition(packet_root=self.packet,before_path=self.before,after_path=self.after,state_dir=self.state,launch_agents_dir=self.launch)
        write(self.after,config('foundation-r25b','b'*40))
        write(self.state/'research-publication-worker-config.json',config('third-party','c'*40))
        with self.assertRaisesRegex(ResearchPublicationGateTransitionError,'CAS'):
            apply(packet_root=self.packet,state_dir=self.state,transition=self.transition)
    def test_rejects_unknown_worker_field(self):
        changed=config('foundation-r25b','b'*40);changed['unknown']=True;write(self.after,changed)
        with self.assertRaisesRegex(ResearchPublicationGateTransitionError,'closed shape'):
            build_transition(packet_root=self.packet,before_path=self.before,after_path=self.after,state_dir=self.state,launch_agents_dir=self.launch)
    def test_rollback_refuses_postinstall_change(self):
        apply(packet_root=self.packet,state_dir=self.state,transition=self.transition)
        changed=config('foundation-r25b','b'*40);changed['workers']=8;write(self.state/'research-publication-worker-config.json',changed)
        with self.assertRaisesRegex(ResearchPublicationGateTransitionError,'changed after'):
            rollback(packet_root=self.packet,state_dir=self.state,transition=self.transition)

    def test_preserved_publication_state_rejects_change_add_remove_and_plist_drift(self):
        expected=self.transition['preserved_publication_state']
        verify_preserved_publication_state(state_dir=self.state,launch_agents_dir=self.launch,expected=expected)
        extra=self.state/'research-localization/extra.json';write(extra,{'x':1})
        with self.assertRaisesRegex(ResearchPublicationGateTransitionError,'state differs'):
            verify_preserved_publication_state(state_dir=self.state,launch_agents_dir=self.launch,expected=expected)
        extra.unlink();(self.state/'research-localization/index.json').unlink()
        with self.assertRaisesRegex(ResearchPublicationGateTransitionError,'state differs'):
            verify_preserved_publication_state(state_dir=self.state,launch_agents_dir=self.launch,expected=expected)
        write(self.state/'research-localization/index.json',{'schema_version':'x'})
        (self.launch/'com.dalton.research-publication-worker.plist').write_bytes(b'changed')
        with self.assertRaisesRegex(ResearchPublicationGateTransitionError,'state differs'):
            verify_preserved_publication_state(state_dir=self.state,launch_agents_dir=self.launch,expected=expected)

    def test_apply_rejects_wrong_mode_without_changing_target(self):
        target=self.state/'research-publication-worker-config.json';original=target.read_bytes();os.chmod(target,0o644)
        with self.assertRaisesRegex(ResearchPublicationGateTransitionError,'CAS'):
            apply(packet_root=self.packet,state_dir=self.state,transition=self.transition)
        self.assertEqual(original,target.read_bytes());self.assertEqual(0o644,target.stat().st_mode&0o777)

    def test_already_rollback_refuses_wrong_mode(self):
        target=self.state/'research-publication-worker-config.json';os.chmod(target,0o644)
        with self.assertRaisesRegex(ResearchPublicationGateTransitionError,'mode differs'):
            rollback(packet_root=self.packet,state_dir=self.state,transition=self.transition)

    def test_full_config_transition_applies_one_gate_mutation(self):
        base=PreserveExistingTransitionTests(methodName='runTest');base.setUp();self.addCleanup(base.doCleanups)
        base.build_pure()
        before=base.packet/'worker.before.json';after=base.packet/'worker.after.json'
        write(before,config('foundation-r25','a'*40));write(after,config('foundation-r25b','b'*40))
        (base.state/'research-localization').mkdir();write(base.state/'research-localization/index.json',{'schema_version':'x'});launch=base.root/'LaunchAgents';launch.mkdir();(launch/'com.dalton.research-publication-worker.plist').write_bytes(b'plist');os.chmod(launch/'com.dalton.research-publication-worker.plist',0o600)
        gate=build_transition(packet_root=base.packet,before_path=before,after_path=after,state_dir=base.state,launch_agents_dir=launch)
        manifest=build_preserve_existing_transition(
            packet_root=base.packet,release_ref='foundation-r25b',source_commit='b'*40,
            baseline_models_path=base.packet/'models.json',
            model_config_paths={name:base.packet/name for name in base.models},
            preserved_config_paths=base.preserved,
            preserved_state_authority_paths={'connector-governance/yfinance-analyst-estimates-v1.json':base.packet/'yfinance-approved.json'},
            service_config_before_path=base.packet/'service.before.json',
            openclaw_config_before_path=base.packet/'openclaw.preserved.json',
            research_publication_gate_transition=gate)
        self.assertEqual(RESEARCH_PUBLICATION_GATE_SCHEMA_VERSION,manifest['schema_version'])
        models,document,lane=expected_transition_state(packet_root=base.packet,manifest=manifest)
        self.assertEqual(base.models,models);self.assertIsInstance(document,dict);self.assertIsInstance(lane,dict)
        base.install_before();(base.state/'research-publication-worker-config.json').write_bytes(before.read_bytes());os.chmod(base.state/'research-publication-worker-config.json',0o600)
        openclaw=base.root/'openclaw.json';openclaw.write_bytes((base.packet/'openclaw.preserved.json').read_bytes())
        manifest_path=base.packet/'gate-manifest.json';write(manifest_path,manifest)
        import hashlib
        receipt=apply_transition_to_scratch(packet_root=base.packet,scratch_root=base.root,state_dir=base.state,
          manifest_path=manifest_path,expected_manifest_sha256=hashlib.sha256(manifest_path.read_bytes()).hexdigest(),
          receipt_path=base.root/'gate-receipt.json',service_config_path=base.service,external_config_path=openclaw)
        self.assertEqual('successor-config-transition-receipt-0.8',receipt['schema_version'])
        self.assertEqual(after.read_bytes(),(base.state/'research-publication-worker-config.json').read_bytes())

    def test_confined_derivation_rewrites_both_gate_artifacts_and_applies(self):
        base=PreserveExistingTransitionTests(methodName='runTest');base.setUp();self.addCleanup(base.doCleanups)
        base.build_pure();before=base.packet/'worker.before.json';after=base.packet/'worker.after.json'
        write(before,config('foundation-r25','a'*40));write(after,config('foundation-r25b','b'*40))
        (base.state/'research-localization').mkdir();write(base.state/'research-localization/index.json',{'schema_version':'x'});launch=base.root/'LaunchAgents';launch.mkdir();(launch/'com.dalton.research-publication-worker.plist').write_bytes(b'plist');os.chmod(launch/'com.dalton.research-publication-worker.plist',0o600)
        gate=build_transition(packet_root=base.packet,before_path=before,after_path=after,state_dir=base.state,launch_agents_dir=launch)
        manifest=build_preserve_existing_transition(packet_root=base.packet,release_ref='foundation-r25b',source_commit='b'*40,
          baseline_models_path=base.packet/'models.json',model_config_paths={n:base.packet/n for n in base.models},
          preserved_config_paths=base.preserved,preserved_state_authority_paths={'connector-governance/yfinance-analyst-estimates-v1.json':base.packet/'yfinance-approved.json'},
          service_config_before_path=base.packet/'service.before.json',openclaw_config_before_path=base.packet/'openclaw.preserved.json',research_publication_gate_transition=gate)
        base.install_before();scratch=base.root/'scratch';(scratch/'openclaw').mkdir(parents=True);(scratch/'openclaw/openclaw.json').write_bytes((base.packet/'openclaw.preserved.json').read_bytes())
        class Module:
            @staticmethod
            def model_config_inventory(state):return {p.name:json.loads(p.read_text()) for p in state.glob('*-model-config.json')}
        rehearsal=SimpleNamespace(temp_root=scratch,temp_state=base.state,temp_config=base.service,replacements={},launch_agents_dir=launch)
        derived_path,_,proof=derive_confined_transition(Module,rehearsal,packet_root=base.packet,manifest=manifest,original_manifest_sha256='e'*64)
        derived=json.loads(derived_path.read_text());self.assertEqual('successor-confined-transition-derivation-0.8',proof['schema_version'])
        self.assertTrue(all(str(scratch) in json.loads((derived_path.parent/derived['research_publication_gate_transition'][side]['file']).read_text())['publication_gate']['release_pointer'] for side in ('before','after')))

if __name__=='__main__':unittest.main()
