import hashlib,json,sqlite3,subprocess,sys,tempfile,unittest
from pathlib import Path
from dalton_core.cockpit_ask_recovery import failed_job_hash
from dalton_core.store import canonical_json
class T(unittest.TestCase):
 def setUp(self):
  self.t=tempfile.TemporaryDirectory();self.r=Path(self.t.name);self.j=self.r/'j.db';self.s=self.r/'s.db'
  c=sqlite3.connect(self.j);c.execute('CREATE TABLE cockpit_jobs(job_id TEXT PRIMARY KEY,kind TEXT,login TEXT,status TEXT,request_json TEXT,result_json TEXT,error TEXT,created_at TEXT,updated_at TEXT)');c.execute('CREATE TABLE cockpit_events(event_id INTEGER PRIMARY KEY,at TEXT,kind TEXT,title TEXT,detail TEXT,login TEXT,refs_json TEXT)');c.execute('insert into cockpit_jobs values(?,?,?,?,?,?,?,?,?)',('job:1','ask','o','failed',json.dumps({'question':'q','request_id':'rid'}),None,'e','t0','t1'));c.commit();c.row_factory=sqlite3.Row;self.h=failed_job_hash(dict(c.execute('select * from cockpit_jobs').fetchone()));c.close()
  raw=canonical_json({'answer':'a','gaps':[]});rh=hashlib.sha256(raw.encode()).hexdigest();env=canonical_json({'metadata':{'route_decision_ref':'route:p'},'outputs':{'text':raw,'content_hash':rh}});eh=hashlib.sha256(env.encode()).hexdigest();c=sqlite3.connect(self.s);c.execute('CREATE TABLE scheduler_result_envelopes(result_envelope_id TEXT PRIMARY KEY,result_envelope_hash TEXT,result_envelope_json TEXT,outcome TEXT,work_order_id TEXT)');c.execute('CREATE TABLE scheduler_work_orders(work_order_id TEXT PRIMARY KEY,work_order_json TEXT,work_order_hash TEXT)');c.execute('insert into scheduler_result_envelopes values(?,?,?,?,?)',('result:p',eh,env,'succeeded','work:p'));work=canonical_json({'metadata':{'request_id':'rid'},'question':'header\n问题：q'});c.execute('insert into scheduler_work_orders values(?,?,?)',('work:p',work,hashlib.sha256(work.encode()).hexdigest()));c.commit();c.close()
  self.p={'question':'q','answer':'a','gaps':[],'replayed':True};self.pf=self.r/'p.json';self.pf.write_text(canonical_json(self.p));self.ps=hashlib.sha256(canonical_json(self.p).encode()).hexdigest()
  c=sqlite3.connect(self.r/'thesis-impact-budget.sqlite');c.execute('create table thesis_impact_day_admissions(admission_id text,work_order_ref text)');c.execute('create table thesis_impact_day_settlements(admission_id text,actual_micros integer)');c.execute('insert into thesis_impact_day_admissions values(?,?)',('a','work:p'));c.execute('insert into thesis_impact_day_settlements values(?,?)',('a',10));c.commit();c.close()
  for n in ('m','c','b','v'): (self.r/f'{n}.json').write_text('{}')
 def cmd(self,ref='result:p',out='out.json',execute=False):
  args=[sys.executable,'scripts/recover_failed_cockpit_ask.py','--journal-db',str(self.j),'--scheduler-db',str(self.s),'--mission',str(self.r/'m.json'),'--checker-config',str(self.r/'c.json'),'--brain-config',str(self.r/'b.json'),'--verifier-config',str(self.r/'v.json'),'--artifact-dir',str(self.r/'art'),'--receipt',str(self.r/'receipt'),'--output',str(self.r/out),'--job-id','job:1','--expected-failed-hash',self.h,'--expected-checker-source-hash',hashlib.sha256((canonical_json({'kind':'ask_answer','version_ref':'cockpit-ask:rid','sections':[{'title':'回答','body':'a','gaps':[]}]})+'\n').encode()).hexdigest(),'--producer-result-envelope-ref',ref,'--producer-route-decision-ref','route:p','--brain-result-envelope-ref','result:b','--brain-raw-sha256','a'*64,'--original-source-commit','a'*40,'--successor-source-commit','b'*40,'--recovered-at','t2']
  if execute:args.append('--execute')
  return subprocess.run(args,cwd=Path(__file__).parents[1],env={**__import__('os').environ,'PYTHONPATH':'src'},capture_output=True,text=True)
 def test_plan_binds_formal_envelope_and_preoccupied_output_refuses_before_mutation(self):
  self.assertEqual(self.cmd().returncode,0);self.assertTrue((self.r/'out.json').exists())
  before=sqlite3.connect(self.j).execute('select status from cockpit_jobs').fetchone()[0]
  self.assertEqual(self.cmd().returncode,0);self.assertEqual(sqlite3.connect(self.j).execute('select status from cockpit_jobs').fetchone()[0],before)
 def test_fullwidth_prompt_detail_is_not_part_of_citation_statement(self):
  import importlib.util
  spec=importlib.util.spec_from_file_location('recover_script',Path('scripts/recover_failed_cockpit_ask.py'));m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
  self.assertEqual(m.shown_from_prompt('C7 [2026Q2] （DXC；收入） 正文。'),[{'tag':'C7','statement':'正文。','ref':None,'period':'2026Q2','company':'','at':'','block':'claims','recovered_prompt_detail':'DXC；收入'}])
 def test_wrong_formal_envelope_ref_refuses(self):
  self.assertNotEqual(self.cmd(ref='result:wrong').returncode,0);self.assertFalse((self.r/'out.json').exists())
 def test_reserved_output_finishes_after_cas_without_model_call(self):
  self.assertEqual(self.cmd(out='resume.json').returncode,0)
  c=sqlite3.connect(self.j);result=canonical_json({'question':'q'});c.execute("update cockpit_jobs set status='done',result_json=?,error=null",(result,));c.commit();c.close()
  receipt={'job_id':'job:1','failed_job_hash':self.h,'result_sha256':hashlib.sha256(result.encode()).hexdigest()};(self.r/'receipt').write_text(canonical_json(receipt))
  got=self.cmd(out='resume.json',execute=True);self.assertEqual(got.returncode,0,got.stderr);self.assertEqual(json.loads((self.r/'resume.json').read_text())['status'],'complete')
if __name__=='__main__':unittest.main()
