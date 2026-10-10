import json,tempfile,unittest
from unittest.mock import patch
from pathlib import Path
from dalton_core.store import DaltonStore,content_hash
from dalton_core.coverage_mission import CoverageMissionAuthority
from dalton_core.industry_archetype_repair import plan_repair,apply_repair,ArchetypeRepairError
from dalton_core.workspace_runtime_setup import generic_driver_pack_template
from tests.p9a_fixtures import bootstrap_method_authorities,mission_params

class ExistingIndustryRepairTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.store=DaltonStore(Path(self.tmp.name)/'core.sqlite');self.addCleanup(self.store.close)
        self.seed=bootstrap_method_authorities(self.store,industry_ref='industry:hyperscalers')
        old=self.seed['pack']
        pack=self.seed['admission'].register_driver_pack(old['driver_pack_ref'],industry_ref='industry:hyperscalers',title=old['title'],**generic_driver_pack_template(),actor_ref='human:test',version_id='pack:generic:2',prior_version_ref=old['id'],idempotency_key='pack:generic:2')
        old=self.seed['constitution']
        self.seed['constitution']=self.seed['constitution_authority'].publish_constitution(old['constitution_ref'],industry_ref='industry:hyperscalers',title=old['title'],bindings={**old['bindings'],'driver_pack_version':{'ref':pack['id'],'hash':pack['content_hash']}},method=old['method'],actor_ref='human:test',version_id='constitution:generic:2',prior_version_ref=old['id'],idempotency_key='constitution:generic:2')
        params=mission_params(self.seed);params['industry_ref']='industry:hyperscalers'
        ref=params.pop('mission_ref');self.mission=CoverageMissionAuthority(self.store).create_mission(ref,**params)

    def test_plan_is_read_only_and_apply_preserves_old_hashes_budgets_and_checkpoints(self):
        db=self.store.connection;before=[tuple(x) for x in db.execute('SELECT mission_version_id,record_json,content_hash FROM coverage_mission_versions')]
        plan=plan_repair(db,self.mission['mission_ref'],include_material=False)
        self.assertEqual(len(plan['specialized_pack']['drivers']),16)
        self.assertEqual(before,[tuple(x) for x in db.execute('SELECT mission_version_id,record_json,content_hash FROM coverage_mission_versions')])
        old_policy={'old':'policy'};(Path(self.tmp.name)/'p12a-dossier-policy-v1.json').write_text(json.dumps(old_policy))
        result=apply_repair(self.store,self.tmp.name,plan,actor_ref='human:test')
        new=CoverageMissionAuthority(self.store).active_mission(self.mission['mission_ref'])
        self.assertEqual(new['prior_version_ref'],self.mission['id']);self.assertEqual(new['budget'],self.mission['budget']);self.assertEqual(new['autonomy'],self.mission['autonomy']);self.assertEqual(new['universe'],self.mission['universe'])
        for row in before:self.assertEqual(tuple(db.execute('SELECT mission_version_id,record_json,content_hash FROM coverage_mission_versions WHERE mission_version_id=?',(row[0],)).fetchone()),row)
        archived=Path(result['previous_policy_snapshots'][0]['path']);self.assertEqual(json.loads(archived.read_text()),old_policy)
        policy=json.loads((Path(self.tmp.name)/'p12a-dossier-policy-v1.json').read_text());self.assertEqual(policy['causal_chain_maps'][0]['sections'].count('supply_and_cost'),8)
        self.assertIn(':archetype-',policy['policy_ref'])
        self.assertEqual(apply_repair(self.store,self.tmp.name,plan,actor_ref='human:test')['status'],'duplicate')

    def test_modified_self_hashed_plan_cannot_raise_owner_budget(self):
        plan=plan_repair(self.store.connection,self.mission['mission_ref'],include_material=False)
        plan['prior_mission']['budget']['max_daily_cost_usd']+=1
        plan['content_hash']=content_hash({k:v for k,v in plan.items() if k!='content_hash'})
        with self.assertRaises(ArchetypeRepairError):apply_repair(self.store,self.tmp.name,plan,actor_ref='human:test')

    def test_automation_cannot_publish_method_repair(self):
        plan=plan_repair(self.store.connection,self.mission['mission_ref'],include_material=False)
        with self.assertRaisesRegex(ArchetypeRepairError,'human authority'):apply_repair(self.store,self.tmp.name,plan,actor_ref='automation:test')

    def test_policy_write_crash_resumes_exact_versions_without_duplicate_append(self):
        plan=plan_repair(self.store.connection,self.mission['mission_ref'],include_material=False)
        with patch('dalton_core.workspace_mission_setup.materialize_first_mission_output_policies',side_effect=RuntimeError('isolated disk failure')):
            with self.assertRaises(RuntimeError):apply_repair(self.store,self.tmp.name,plan,actor_ref='human:test')
        counts={t:self.store.connection.execute('SELECT count(*) FROM '+t).fetchone()[0] for t in ('driver_pack_versions','research_constitution_versions','coverage_mission_versions')}
        result=apply_repair(self.store,self.tmp.name,plan,actor_ref='human:test')
        self.assertEqual(result['status'],'applied')
        self.assertEqual(counts,{t:self.store.connection.execute('SELECT count(*) FROM '+t).fetchone()[0] for t in counts})

    def test_corrupted_task_refuses_before_any_authority_append(self):
        plan=plan_repair(self.store.connection,self.mission['mission_ref'],include_material=False)
        plan['dossier_tasks']=[{'status':'prepared_for_independent_verification','content_hash':'a'*64}]
        plan['content_hash']=content_hash({k:v for k,v in plan.items() if k!='content_hash'})
        before=self.store.connection.execute('SELECT count(*) FROM driver_pack_versions').fetchone()[0]
        with self.assertRaisesRegex(ArchetypeRepairError,'task drifted'):apply_repair(self.store,self.tmp.name,plan,actor_ref='human:test')
        self.assertEqual(self.store.connection.execute('SELECT count(*) FROM driver_pack_versions').fetchone()[0],before)

    def test_cli_defaults_to_read_only_review_without_new_versions(self):
        from dalton_core.industry_archetype_repair_cli import main
        output=Path(self.tmp.name)/'review-plan.json'
        before=self.store.connection.execute('SELECT count(*) FROM coverage_mission_versions').fetchone()[0]
        self.assertEqual(main(['--state-dir',self.tmp.name,'--mission',self.mission['mission_ref'],'--output',str(output)]),0)
        plan=json.loads(output.read_text());self.assertEqual(plan['status'],'ready')
        self.assertEqual(before,self.store.connection.execute('SELECT count(*) FROM coverage_mission_versions').fetchone()[0])
        self.assertTrue(all(r['status']=='material_required' for r in plan['dossier_tasks']))
