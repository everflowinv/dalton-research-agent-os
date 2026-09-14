import json,sqlite3,tempfile,unittest
from pathlib import Path
from dalton_core.cockpit_ask_recovery import failed_job_hash,recover_failed_ask_job,AskRecoveryError
from dalton_core.store import canonical_json,content_hash
class T(unittest.TestCase):
 def setUp(self):
  self.t=tempfile.TemporaryDirectory();self.root=Path(self.t.name);self.c=sqlite3.connect(':memory:');self.c.row_factory=sqlite3.Row
  self.c.executescript('CREATE TABLE cockpit_jobs(job_id TEXT PRIMARY KEY,kind TEXT,login TEXT,status TEXT,request_json TEXT,result_json TEXT,error TEXT,created_at TEXT,updated_at TEXT);CREATE TABLE cockpit_events(event_id INTEGER PRIMARY KEY,at TEXT,kind TEXT,title TEXT,detail TEXT,login TEXT,refs_json TEXT);')
  self.c.execute("insert into cockpit_jobs values(?,?,?,?,?,?,?,?,?)",('job:1','ask','owner','failed',json.dumps({'question':'问题','request_id':'r'}),None,'pending','t0','t1'));self.c.commit();self.h=failed_job_hash(dict(self.c.execute('select * from cockpit_jobs').fetchone()))
  review={'status':'ready_for_publication','brain_revision':{'sections':[{'body':'审校回答','gaps':['审校缺口']}]},'review_cost_micros':30,'brain_recovery':{'checker_stage_sha256':'d'*64,'brain_result_envelope_ref':'result:b','normalization':{'raw_sha256':'e'*64,'fixed_sha256':'f'*64,'suffix':']}'}}};review['content_hash']=content_hash(review);data=(canonical_json(review)+'\n').encode();self.art=self.root/'final.json';self.art.write_bytes(data)
  import hashlib
  self.producer={'question':'问题','answer':'原回答','sentences':[],'citations':[],'gaps':['原缺口'],'unknowns':[],'confidence':'medium','refused':False,'refusal_reason':None,'refusal_label':None,'refusal_detail':None,'market_vs_us':None,'verification':{},'context':{},'answer_policy':{},'refresh':{},'refreshed_with':[],'refresh_note':None,'claims_considered':1,'claims_total':1,'duplicates_dropped':0,'cost_usd':0.1}
  self.result={**self.producer,'display_answer':'审校回答','display_gaps':['审校缺口'],'display_gap_details':[],'cost_usd':0.1,'language_review':{'status':'ready_for_publication','artifact_ref':'final','artifact_sha256':hashlib.sha256(data).hexdigest()}}
  self.proof={'schema_version':'cockpit-ask-language-recovery:0.1','job_id':'job:1','failed_job_hash':self.h,'original_source_commit':'a'*40,'successor_source_commit':'b'*40,'language_artifact_ref':'final','language_artifact_sha256':hashlib.sha256(data).hexdigest(),'producer_result_envelope_ref':'result:p','producer_result_sha256':hashlib.sha256(canonical_json(self.producer).encode()).hexdigest(),'checker_stage_sha256':'d'*64,'brain_result_envelope_ref':'result:b','brain_raw_sha256':'e'*64,'brain_fixed_sha256':'f'*64,'brain_suffix':']}'}
 def tearDown(self):self.t.cleanup()
 def call(self,**kw):return recover_failed_ask_job(self.c,job_id='job:1',expected_failed_hash=self.h,producer_result=self.producer,result=self.result,recovery_proof=self.proof,artifact_dir=self.root,receipt_path=self.root/'receipt.json',recovered_at='t2',**kw)
 def test_exact_cas_and_durable_receipt(self):
  out=self.call();self.assertEqual(out['status'],'done');self.assertTrue((self.root/'receipt.json').is_file());self.assertEqual(self.c.execute('select status from cockpit_jobs').fetchone()[0],'done')
 def test_tampered_artifact_body_and_ref_refuse(self):
  for mutation in ('body','ref','artifact'):
   result=json.loads(json.dumps(self.result));proof=dict(self.proof)
   if mutation=='body':result['display_answer']='篡改'
   elif mutation=='ref':result['language_review']['artifact_ref']='other'
   else:self.art.write_text('{}')
   with self.subTest(mutation=mutation),self.assertRaises(AskRecoveryError):recover_failed_ask_job(self.c,job_id='job:1',expected_failed_hash=self.h,producer_result=self.producer,result=result,recovery_proof=proof,artifact_dir=self.root,receipt_path=self.root/f'{mutation}.json',recovered_at='t2')
   if mutation=='artifact':break
  self.assertEqual(self.c.execute('select status from cockpit_jobs').fetchone()[0],'failed')
if __name__=='__main__':unittest.main()
