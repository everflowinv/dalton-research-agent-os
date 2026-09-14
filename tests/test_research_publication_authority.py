import hashlib,json,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from dalton_core.research_publication_authority import published_runtime_gate
from dalton_core import research_output_preparation as prep

class PublicationGateTests(unittest.TestCase):
 def setUp(self):
    self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
    self.root=Path(self.temp.name).resolve();self.r=self.root/'current-release.json';self.c=self.root/'current-runtime-config.json'
    self.cfg={'release_pointer':str(self.r),'runtime_pointer':str(self.c),
              'expected_release_ref':'foundation-r25','expected_source_commit':'a'*40}
 def write(self,source='a'*40):
    c={'schema_version':'dalton-runtime-config-pointer-0.2','status':'deployed_verified','base_release_commit':source,'candidate_manifest_sha256':'b'*64}
    self.c.write_text(json.dumps(c))
    r={'schema_version':'dalton-current-release-0.2','status':'deployed_verified','source_commit':source,
       'release_ref':'foundation-r25','candidate_manifest_sha256':'b'*64,
       'current_runtime_config_sha256':hashlib.sha256(self.c.read_bytes()).hexdigest()}
    self.r.write_text(json.dumps(r))
 def test_missing_predecessor_and_intermediate_publication_do_not_activate(self):
    self.assertEqual(published_runtime_gate(self.cfg)['status'],'waiting_for_release_publication')
    self.write('c'*40);self.assertEqual(published_runtime_gate(self.cfg)['status'],'waiting_for_release_publication')
    self.write();self.c.write_text(self.c.read_text()+'\n')
    self.assertEqual(published_runtime_gate(self.cfg)['status'],'waiting_for_release_publication')
 def test_only_the_exact_final_pointer_pair_activates(self):
    self.write();before=[self.r.read_bytes(),self.c.read_bytes()]
    self.assertEqual(published_runtime_gate(self.cfg)['status'],'active')
    self.assertEqual(before,[self.r.read_bytes(),self.c.read_bytes()])
 def test_an_exact_immutable_release_hash_can_activate(self):
    release_ref='release:sha256:'+'d'*64
    self.cfg['expected_release_ref']=release_ref
    self.write()
    release=json.loads(self.r.read_text());release['release_ref']=release_ref
    self.r.write_text(json.dumps(release))
    self.assertEqual(published_runtime_gate(self.cfg)['status'],'active')
    for malformed in ('release:sha256:'+'d'*63, 'release:sha256:'+'G'*64,
                      'release:sha256:'+'d'*64+':extra'):
        with self.subTest(malformed=malformed):
            self.assertEqual(
                published_runtime_gate({**self.cfg,'expected_release_ref':malformed})['status'],
                'invalid_release_authority',
            )
 def test_invalid_pointer_and_symlink_fail_closed(self):
    self.write();self.r.unlink();self.r.symlink_to(self.c)
    self.assertEqual(published_runtime_gate(self.cfg)['status'],'invalid_release_authority')
    self.assertEqual(published_runtime_gate({})['status'],'invalid_release_authority')

 def worker_config(self):
    cfg={name:str(self.root/name) for name in ('core_db','scheduler_db','model_config',
        'verifier_config','checker_config','brain_config','work_dir','output_directory')}
    cfg.update(schema_version='research-publication-worker-config:0.1',workers=4,
        chunk_chars=4500,max_cost_per_call=1.0,draft_attempts=2,publication_gate=self.cfg)
    return cfg

 def test_waiting_worker_never_opens_database_or_model(self):
    cfg=self.worker_config(); path=self.root/'worker.json';path.write_text(json.dumps(cfg))
    with patch.object(prep.sqlite3,'connect',side_effect=AssertionError('database opened')), \
         patch.object(prep,'CockpitModel',side_effect=AssertionError('model opened')):
        self.assertEqual(prep.run_worker(path),0)
    result=json.loads((Path(cfg['work_dir'])/'worker-last-run.json').read_text())
    self.assertEqual(result['status'],'waiting_for_release_publication')
    self.assertEqual(result['model_calls'],0)
    self.assertEqual(set(result),{'schema_version','status','model_calls',
        'observed_release_sha256','observed_runtime_sha256','checked_at'})

 def test_worker_rejects_unknown_fields_and_invalid_bounds_before_io(self):
    for changes in ({'extra':True},{'workers':True},{'workers':9},
                    {'max_cost_per_call':float('nan')},{'max_cost_per_call':2},
                    {'core_db':'relative.sqlite'}):
        with self.subTest(changes=changes):
            cfg={**self.worker_config(),**changes}
            with self.assertRaises(ValueError):prep.validate_worker_config(cfg)
