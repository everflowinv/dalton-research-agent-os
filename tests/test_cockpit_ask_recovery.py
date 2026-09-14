import json,sqlite3,unittest
from dalton_core.cockpit_ask_recovery import failed_job_hash,recover_failed_ask_job,AskRecoveryError
class T(unittest.TestCase):
 def setUp(self):
  self.c=sqlite3.connect(':memory:');self.c.row_factory=sqlite3.Row
  self.c.executescript('CREATE TABLE cockpit_jobs(job_id TEXT PRIMARY KEY,kind TEXT,login TEXT,status TEXT,request_json TEXT,result_json TEXT,error TEXT,created_at TEXT,updated_at TEXT);CREATE TABLE cockpit_events(event_id INTEGER PRIMARY KEY,at TEXT,kind TEXT,title TEXT,detail TEXT,login TEXT,refs_json TEXT);')
  self.c.execute("insert into cockpit_jobs values(?,?,?,?,?,?,?,?,?)",('job:1','ask','owner','failed',json.dumps({'question':'问题','request_id':'r'}),None,'pending','t0','t1'));self.c.commit()
  self.row=dict(self.c.execute('select * from cockpit_jobs').fetchone());self.h=failed_job_hash(self.row)
  self.proof={'schema_version':'cockpit-ask-language-recovery:0.1','job_id':'job:1','failed_job_hash':self.h,'original_source_commit':'a'*40,'successor_source_commit':'b'*40,'language_artifact_ref':'x','language_artifact_sha256':'c'*64,'producer_result_envelope_ref':'result:p','checker_stage_sha256':'d'*64,'brain_result_envelope_ref':'result:b','brain_raw_sha256':'e'*64,'brain_fixed_sha256':'f'*64,'brain_suffix':']}'}
  self.result={'question':'问题','answer':'回答','citations':[],'language_review':{'status':'ready_for_publication'}}
 def test_exact_cas_updates_same_job_and_records_proof(self):
  out=recover_failed_ask_job(self.c,job_id='job:1',expected_failed_hash=self.h,result=self.result,recovery_proof=self.proof,recovered_at='t2')
  self.assertEqual(self.c.execute('select status from cockpit_jobs').fetchone()[0],'done');self.assertEqual(self.c.execute('select count(*) from cockpit_events').fetchone()[0],1);self.assertEqual(out['status'],'done')
  with self.assertRaises(AskRecoveryError):recover_failed_ask_job(self.c,job_id='job:1',expected_failed_hash=self.h,result=self.result,recovery_proof=self.proof,recovered_at='t3')
 def test_drift_wrong_question_and_unreviewed_refuse_without_write(self):
  for result,proof,h in ((dict(self.result,question='别的问题'),self.proof,self.h),(dict(self.result,language_review={'status':'pending'}),self.proof,self.h),(self.result,self.proof,'0'*64)):
   with self.subTest(result=result,h=h),self.assertRaises(AskRecoveryError):recover_failed_ask_job(self.c,job_id='job:1',expected_failed_hash=h,result=result,recovery_proof=proof,recovered_at='t2')
  self.assertEqual(self.c.execute('select status from cockpit_jobs').fetchone()[0],'failed');self.assertEqual(self.c.execute('select count(*) from cockpit_events').fetchone()[0],0)
if __name__=='__main__':unittest.main()
