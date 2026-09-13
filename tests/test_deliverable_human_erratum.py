import argparse, hashlib, json, sqlite3, tempfile, unittest
from pathlib import Path
from unittest.mock import patch
from dalton_core.mission_deliverable import MissionDeliverableAuthority, MissionDeliverableConflict
from dalton_core.store import canonical_json, content_hash
from tests.test_deliverable_reopen import ReopenHarness, AUTOMATION, OWNER

class HumanErratumAppendTests(ReopenHarness):
    def fixture(self):
        sections=self.sections();sections[3]['body']+=' A-old B-old C-old D-old E-old'
        source=self.publish(sections=sections);self.pass_gate(source)
        changes=[{'before':f'{x}-old','after':f'{x}-new'} for x in 'ABCDE']
        after=source['sections'][3]['body']
        for x in changes:after=after.replace(x['before'],x['after'])
        claims=[]
        for ref in self.claim_refs[:4]:
            h=self.store.connection.execute('select content_hash from claim_versions where claim_version_id=?',(ref,)).fetchone()[0]
            claims.append({'claim_version_ref':ref,'claim_content_hash':h})
        erratum={'candidate_file_sha256':'a'*64,'candidate_hash':'b'*64,
          'source_version_ref':source['id'],'source_version_hash':source['content_hash'],'source_version_number':1,
          'section_index':3,'before_sha256':hashlib.sha256(source['sections'][3]['body'].encode()).hexdigest(),
          'after_sha256':hashlib.sha256(after.encode()).hexdigest(),'substitutions':changes,'claim_authorities':claims}
        proposal={'schema_version':'0.1','id':'gate-reopen-proposal:erratum','created_at':'2026-09-13T00:00:00+00:00',
          'company_ref':source['subject_ref'],'deliverable_ref':source['deliverable_ref'],'stage_ref':'initial_screen',
          'stage_record_ref':'stage','passed_version_ref':source['id'],'passed_version_hash':source['content_hash'],
          'passed_version_number':1,'passed_at':source['created_at'],'assessment_hash':'c'*64,
          'policy_ref':'owner-directed-factual-erratum:0.1','thresholds':{},'flipped':[],'regressed':[],
          'diff':[],'evidence_refs':self.claim_refs[:4],'gate_recomputed':None,'checkpoint_kind':'gate_reopen',
          'change_reason':'human_revision','mission_version_ref':self.mission['id'],
          'mission_version_hash':self.mission['content_hash'],'actor_ref':AUTOMATION,'erratum':erratum}
        proposal['content_hash']=content_hash(proposal)
        decision={'schema_version':'0.1','id':'gate-reopen-decision:erratum','created_at':'2026-09-13T00:01:00+00:00',
          'proposal_ref':proposal['id'],'proposal_hash':proposal['content_hash'],'company_ref':source['subject_ref'],
          'deliverable_ref':source['deliverable_ref'],'passed_version_ref':source['id'],'verdict':'approve',
          'reason':'纠正期间','change_reason':'human_revision','evidence_refs':self.claim_refs[:4],
          'reviewer_ref':OWNER,'actor_ref':OWNER}
        decision['content_hash']=content_hash(decision)
        with self.store._transaction() as cur:
            cur.execute('INSERT INTO gate_reopen_proposals VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
              (proposal['id'],proposal['company_ref'],proposal['deliverable_ref'],'initial_screen',source['id'],source['content_hash'],proposal['assessment_hash'],0,'gate_reopen','human_revision',self.mission['id'],canonical_json(proposal),proposal['content_hash'],AUTOMATION,proposal['created_at']))
            cur.execute('INSERT INTO gate_reopen_decisions VALUES(?,?,?,?,?,?,?,?,?,?)',
              (decision['id'],proposal['id'],proposal['content_hash'],proposal['company_ref'],'approve',decision['reason'],canonical_json(decision),decision['content_hash'],OWNER,decision['created_at']))
        return source,{**decision,'proposal':proposal},after

    def test_only_the_five_substitutions_are_appended(self):
        source,permission,after=self.fixture(); authority=MissionDeliverableAuthority(self.store)
        result=authority.publish_human_erratum(permission=permission,mission=self.mission,playbook=self.playbook,actor_ref=AUTOMATION)
        self.assertEqual(result['version'],2);self.assertEqual(result['prior_version_ref'],source['id'])
        self.assertEqual(result['sections'][3]['body'],after)
        self.assertEqual([x for i,x in enumerate(result['sections']) if i!=3],[x for i,x in enumerate(source['sections']) if i!=3])
        old=self.store.connection.execute('select record_json from mission_deliverable_versions where version_id=?',(source['id'],)).fetchone()[0]
        self.assertEqual(json.loads(old),{k:v for k,v in source.items() if k!='status'})
        replay=authority.publish_human_erratum(permission=permission,mission=self.mission,playbook=self.playbook,actor_ref=AUTOMATION)
        self.assertEqual(replay['status'],'duplicate');self.assertEqual(replay['id'],result['id'])
        later=authority.publish(kind='initial_screen',subject_ref=source['subject_ref'],mission=self.mission,
          playbook=self.playbook,template_ref=source['template_ref'],sections=result['sections'],summary=result['summary']+' 后续',
          gaps=result['gaps'],model_invocation_refs=result['model_invocation_refs'],actor_ref=OWNER,
          revision={'change_reason':'evidence_thicker','evidence_refs':[self.claim_refs[0]]})
        self.assertEqual(later['version'],3)
        with self.assertRaisesRegex(MissionDeliverableConflict,'no longer'):
            authority.publish_human_erratum(permission=permission,mission=self.mission,playbook=self.playbook,actor_ref=AUTOMATION)

    def test_fake_or_stale_permission_cannot_publish(self):
        _source,permission,_after=self.fixture();permission['proposal']['erratum']['after_sha256']='0'*64
        with self.assertRaises(MissionDeliverableConflict):
            MissionDeliverableAuthority(self.store).publish_human_erratum(permission=permission,mission=self.mission,playbook=self.playbook,actor_ref=AUTOMATION)


class ReadOnlyPlanTests(unittest.TestCase):
    def test_plan_uses_sqlite_read_only_and_never_constructs_dalton_store(self):
        from dalton_core.deliverable_erratum_execution import run
        with tempfile.TemporaryDirectory() as tmp:
            state=Path(tmp);db=sqlite3.connect(state/'core.sqlite')
            db.execute('create table mission_deliverable_versions(version_id text,mission_version_ref text,content_hash text,version_number integer)')
            db.execute('create table coverage_mission_versions(mission_version_id text,mission_ref text,record_json text,content_hash text,actor_ref text)')
            db.execute('create table coverage_mission_pointer(mission_ref text,mission_version_id text)')
            old={'id':'mission:v1','mission_ref':'mission:one','content_hash':'o'*64,'autonomy':{'automation_principal':AUTOMATION}}
            mission={'id':'mission:v2','mission_ref':'mission:one','content_hash':'m'*64,'autonomy':{'automation_principal':AUTOMATION}}
            db.execute('insert into mission_deliverable_versions values(?,?,?,?)',('version:v1','mission:v1','s'*64,1))
            db.execute('insert into coverage_mission_versions values(?,?,?,?,?)',('mission:v1','mission:one',json.dumps(old),'o'*64,OWNER))
            db.execute('insert into coverage_mission_versions values(?,?,?,?,?)',('mission:v2','mission:one',json.dumps(mission),'m'*64,OWNER))
            db.execute('insert into coverage_mission_pointer values(?,?)',('mission:one','mission:v2'));db.commit();db.close()
            candidate={'source_version':{'version_ref':'version:v1','content_hash':'s'*64,'version_number':1},'subject_ref':'company:x'}
            path=state/'candidate.json';wire=json.dumps(candidate).encode();path.write_bytes(wire)
            args=argparse.Namespace(candidate=path,expected_candidate_sha256=hashlib.sha256(wire).hexdigest(),state_dir=state,
              execute=False,expected_automation_actor=AUTOMATION,owner_actor=OWNER)
            with patch('dalton_core.deliverable_erratum_execution.DaltonStore',side_effect=AssertionError('writer opened')):
                planned=run(args)
                self.assertEqual(planned['status'],'planned_read_only')
                self.assertEqual(planned['source_mission_version_ref'],'mission:v1')
                self.assertEqual(planned['mission_version_ref'],'mission:v2')
