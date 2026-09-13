from __future__ import annotations
import json, os, tempfile, unittest
from pathlib import Path
from types import SimpleNamespace

from scripts.successor_research_publication_gate_transition import (
    ResearchPublicationGateTransitionError, apply, build_transition, expected_state, rollback,
)
from scripts.prepare_successor_config_transition import (
    RESEARCH_PUBLICATION_GATE_SCHEMA_VERSION, apply_transition_to_scratch,
    build_preserve_existing_transition,
)
from tests.test_successor_config_transition import PreserveExistingTransitionTests
from scripts.run_successor_copied_state_rehearsal import derive_confined_transition

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
        t=tempfile.TemporaryDirectory();self.addCleanup(t.cleanup);self.root=Path(t.name);self.packet=self.root/'packet';self.packet.mkdir();self.state=self.root/'state';self.state.mkdir()
        self.before=self.packet/'before.json';self.after=self.packet/'after.json'
        write(self.before,config('foundation-r25','a'*40));write(self.after,config('foundation-r25b','b'*40))
        self.transition=build_transition(packet_root=self.packet,before_path=self.before,after_path=self.after)
        (self.state/'research-publication-worker-config.json').write_bytes(self.before.read_bytes());os.chmod(self.state/'research-publication-worker-config.json',0o600)
    def test_apply_and_idempotent_rollback_preserve_every_other_value(self):
        proof=apply(packet_root=self.packet,state_dir=self.state,transition=self.transition)
        self.assertEqual(self.after.read_bytes(),(self.state/'research-publication-worker-config.json').read_bytes())
        self.assertEqual(1,proof['configuration_mutations'])
        self.assertEqual('rolled_back',rollback(packet_root=self.packet,state_dir=self.state,transition=self.transition)['status'])
        self.assertEqual(0o600,(self.state/'research-publication-worker-config.json').stat().st_mode&0o777)
        self.assertEqual('already_rolled_back',rollback(packet_root=self.packet,state_dir=self.state,transition=self.transition)['status'])
    def test_rejects_non_gate_change_and_cas_drift(self):
        changed=config('foundation-r25b','b'*40);changed['workers']=8;write(self.after,changed)
        with self.assertRaisesRegex(ResearchPublicationGateTransitionError,'only publication gate'):
            build_transition(packet_root=self.packet,before_path=self.before,after_path=self.after)
        write(self.after,config('foundation-r25b','b'*40))
        write(self.state/'research-publication-worker-config.json',config('third-party','c'*40))
        with self.assertRaisesRegex(ResearchPublicationGateTransitionError,'CAS'):
            apply(packet_root=self.packet,state_dir=self.state,transition=self.transition)
    def test_rejects_unknown_worker_field(self):
        changed=config('foundation-r25b','b'*40);changed['unknown']=True;write(self.after,changed)
        with self.assertRaisesRegex(ResearchPublicationGateTransitionError,'closed shape'):
            build_transition(packet_root=self.packet,before_path=self.before,after_path=self.after)
    def test_rollback_refuses_postinstall_change(self):
        apply(packet_root=self.packet,state_dir=self.state,transition=self.transition)
        changed=config('foundation-r25b','b'*40);changed['workers']=8;write(self.state/'research-publication-worker-config.json',changed)
        with self.assertRaisesRegex(ResearchPublicationGateTransitionError,'changed after'):
            rollback(packet_root=self.packet,state_dir=self.state,transition=self.transition)

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
        gate=build_transition(packet_root=base.packet,before_path=before,after_path=after)
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
        gate=build_transition(packet_root=base.packet,before_path=before,after_path=after)
        manifest=build_preserve_existing_transition(packet_root=base.packet,release_ref='foundation-r25b',source_commit='b'*40,
          baseline_models_path=base.packet/'models.json',model_config_paths={n:base.packet/n for n in base.models},
          preserved_config_paths=base.preserved,preserved_state_authority_paths={'connector-governance/yfinance-analyst-estimates-v1.json':base.packet/'yfinance-approved.json'},
          service_config_before_path=base.packet/'service.before.json',openclaw_config_before_path=base.packet/'openclaw.preserved.json',research_publication_gate_transition=gate)
        base.install_before();scratch=base.root/'scratch';(scratch/'openclaw').mkdir(parents=True);(scratch/'openclaw/openclaw.json').write_bytes((base.packet/'openclaw.preserved.json').read_bytes())
        class Module:
            @staticmethod
            def model_config_inventory(state):return {p.name:json.loads(p.read_text()) for p in state.glob('*-model-config.json')}
        rehearsal=SimpleNamespace(temp_root=scratch,temp_state=base.state,temp_config=base.service,replacements={})
        derived_path,_,proof=derive_confined_transition(Module,rehearsal,packet_root=base.packet,manifest=manifest,original_manifest_sha256='e'*64)
        derived=json.loads(derived_path.read_text());self.assertEqual('successor-confined-transition-derivation-0.8',proof['schema_version'])
        self.assertTrue(all(str(scratch) in json.loads((derived_path.parent/derived['research_publication_gate_transition'][side]['file']).read_text())['publication_gate']['release_pointer'] for side in ('before','after')))

if __name__=='__main__':unittest.main()
