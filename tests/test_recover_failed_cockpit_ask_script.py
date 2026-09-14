import hashlib,json,sqlite3,subprocess,sys,tempfile,unittest
from pathlib import Path
from dalton_core.cockpit_ask_recovery import failed_job_hash
from dalton_core.store import canonical_json
class T(unittest.TestCase):
 def setUp(self):
  self.t=tempfile.TemporaryDirectory();self.r=Path(self.t.name);self.j=self.r/'j.db';self.s=self.r/'s.db'
  c=sqlite3.connect(self.j);c.execute('CREATE TABLE cockpit_jobs(job_id TEXT PRIMARY KEY,kind TEXT,login TEXT,status TEXT,request_json TEXT,result_json TEXT,error TEXT,created_at TEXT,updated_at TEXT)');c.execute('CREATE TABLE cockpit_events(event_id INTEGER PRIMARY KEY,at TEXT,kind TEXT,title TEXT,detail TEXT,login TEXT,refs_json TEXT)');c.execute('insert into cockpit_jobs values(?,?,?,?,?,?,?,?,?)',('job:1','ask','o','failed',json.dumps({'question':'q','request_id':'rid'}),None,'e','t0','t1'));c.commit();c.row_factory=sqlite3.Row;self.h=failed_job_hash(dict(c.execute('select * from cockpit_jobs').fetchone()));c.close()
  raw=canonical_json({'answer':'a','gaps':[]});rh=hashlib.sha256(raw.encode()).hexdigest();env=canonical_json({'metadata':{'route_decision_ref':'route:p'},'outputs':{'text':raw,'content_hash':rh}});eh=hashlib.sha256(env.encode()).hexdigest();c=sqlite3.connect(self.s);c.execute('CREATE TABLE scheduler_result_envelopes(result_envelope_id TEXT PRIMARY KEY,result_envelope_hash TEXT,result_envelope_json TEXT,outcome TEXT,work_order_id TEXT)');c.execute('insert into scheduler_result_envelopes values(?,?,?,?,?)',('result:p',eh,env,'succeeded','work:p'));c.commit();c.close()
  self.p={'question':'q','answer':'a','gaps':[],'replayed':True};self.pf=self.r/'p.json';self.pf.write_text(canonical_json(self.p));self.ps=hashlib.sha256(canonical_json(self.p).encode()).hexdigest()
  for n in ('m','c','b','v'): (self.r/f'{n}.json').write_text('{}')
 def cmd(self,ref='result:p',out='out.json'):
  args=[sys.executable,'scripts/recover_failed_cockpit_ask.py','--journal-db',str(self.j),'--scheduler-db',str(self.s),'--producer-result',str(self.pf),'--mission',str(self.r/'m.json'),'--checker-config',str(self.r/'c.json'),'--brain-config',str(self.r/'b.json'),'--verifier-config',str(self.r/'v.json'),'--artifact-dir',str(self.r/'art'),'--receipt',str(self.r/'receipt'),'--output',str(self.r/out),'--job-id','job:1','--expected-failed-hash',self.h,'--expected-producer-sha256',self.ps,'--producer-result-envelope-ref',ref,'--producer-route-decision-ref','route:p','--brain-result-envelope-ref','result:b','--brain-raw-sha256','a'*64,'--original-source-commit','a'*40,'--successor-source-commit','b'*40,'--recovered-at','t2']
  return subprocess.run(args,cwd=Path(__file__).parents[1],env={**__import__('os').environ,'PYTHONPATH':'src'},capture_output=True,text=True)
 def test_plan_binds_formal_envelope_and_preoccupied_output_refuses_before_mutation(self):
  self.assertEqual(self.cmd().returncode,0);self.assertTrue((self.r/'out.json').exists())
  before=sqlite3.connect(self.j).execute('select status from cockpit_jobs').fetchone()[0]
  self.assertNotEqual(self.cmd().returncode,0);self.assertEqual(sqlite3.connect(self.j).execute('select status from cockpit_jobs').fetchone()[0],before)
 def test_wrong_formal_envelope_ref_refuses(self):
  self.assertNotEqual(self.cmd(ref='result:wrong').returncode,0);self.assertFalse((self.r/'out.json').exists())
if __name__=='__main__':unittest.main()
