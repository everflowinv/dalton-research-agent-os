import copy,json,unittest
from unittest.mock import patch
from dalton_core.store import content_hash,canonical_json
from dalton_core.directed_numeric_failure_diagnostics import prior_same_failure
from dalton_core.directed_numeric_period_replacement import binding_for,derive,consumer
from dalton_core.mission_document_research_admission import admit_directed_inquiry
from dalton_core.research_question_backlog import ResearchQuestionBacklog
from dalton_core.directed_numeric_output import candidate_ref
from tests import test_directed_numeric_workflow as fixtures

class NumericFailureDiagnosticsTests(unittest.TestCase):
 def fixture(self,**kwargs):
  h=fixtures.NumericWorkflowTests('runTest');self.addCleanup(h.doCleanups)
  return h,h.fixture(**kwargs)
 def stop(self,e,a):
  for _ in range(20):
   r=e.run_once(a['id'])
   if r['status'] in {'blocked','complete','stopped'}:return r
  self.fail('bounded execution did not stop')
 def many_attempts_fixture(self):
  from tests import test_mission_annual_research as support
  original=support.MissionAnnualFixture.__init__
  def configured(owner,*args,**kwargs):
   original(owner,*args,**kwargs)
   path=owner.state/support.DRAFT_MODEL_CONFIG_NAME
   value=json.loads(path.read_text());value['run_budget']['max_units']=4
   path.write_text(canonical_json(value)+'\n')
  with patch.object(support.MissionAnnualFixture,'__init__',configured):return self.fixture()
 def test_repeated_exact_typed_failure_stops_second_attempt_preserving_both_paid_results(self):
  h,(f,a,ad,e,p,v)=self.many_attempts_fixture()
  p.candidate_wire['targets'][0]['figure']['scale']='millions'
  r=self.stop(e,ad)
  self.assertEqual(p.calls,4) # 2 original attempts + 2 attempts with changed complete diagnostics; no identical second repair
  self.assertEqual(v.calls,0)
  rows=f.store.connection.execute("SELECT work_order_id,attempt_number,result_envelope_json FROM scheduler_formal_results WHERE json_extract(result_envelope_json,'$.metadata.numeric_repeated_contract_failure') IS NOT NULL").fetchall()
  self.assertEqual(len(rows),2)
  for row in rows:
   envelope=json.loads(row['result_envelope_json']);self.assertEqual(row['attempt_number'],2)
   self.assertEqual(envelope['error']['code'],'MODEL_OUTPUT_CONTRACT_REJECTED')
   self.assertTrue(envelope['metadata']['rejected_model_output']['outputs']['text'])
  before=p.calls;self.stop(e,ad);self.assertEqual(p.calls,before)
 def old_impossible(self):
  with patch('dalton_core.directed_numeric_research.assert_executable_target_periods'):
   h,(f,a,ad,e,p,v)=self.fixture(period='文件所载最近报告期末',extra_text=' The disclosure is as of December 31, 2025.')
  with patch('dalton_core.directed_numeric_research.unexecutable_period_targets',return_value=[]),patch('dalton_core.transcript_polish_model_worker.RoutedTranscriptPolishModelWorker._bounded_failure_status',return_value='failed'):
   for _ in range(8):
    r=e.run_once(ad['id'])
    if p.calls:break
  r=self.stop(e,ad)
  self.assertEqual((p.calls,v.calls),(1,0))
  return h,f,a,ad,e,p,v
 def revised(self,h,f,a,ad,binding):
  inquiry=copy.deepcopy(ad['planner_inquiry']);inquiry['rank']=0
  inquiry['directed_document']['replaces_numeric_contract_failure']=binding
  for target in inquiry['directed_document']['numeric_targets']:target['period']='截至 2025-12-31'
  from dalton_core.mission_document_research import _exact_plan
  held=_exact_plan(f.store.connection,ad['plan_ref'])[0];plan={k:copy.deepcopy(held[k]) for k in ('schema_version','task_ref','created_at','state_hash','mission_version_ref','assessment','directives','inquiries','sufficiency')}
  plan['state_hash']='4'*64;plan['mission_version_ref']=f.mission['id'];plan['inquiries']=[inquiry];plan['content_hash']=content_hash(plan)
  saved=h._record_plan(f,plan)
  return inquiry,saved
 def test_old_failure_normal_plan_one_bound_replacement_and_complete_free_replay(self):
  h,f,a,ad,e,p,v=self.old_impossible();connection=f.store.connection
  frozen=[tuple(row) for row in connection.execute('SELECT work_order_id,work_order_json,work_order_hash FROM scheduler_work_orders')]
  binding=binding_for(connection,ad['id']);self.assertIsNotNone(binding)
  from dalton_core.document_research_feedback import read_document_research_feedback
  feedback=read_document_research_feedback(f.store,f.mission)
  self.assertEqual(feedback['status'],'available',feedback)
  failure=next(row for row in feedback['by_company'][ad['company_ref']] if row.get('replaces_numeric_contract_failure'))
  self.assertEqual(failure['replaces_numeric_contract_failure'],binding)
  inquiry,plan=self.revised(h,f,a,ad,binding)
  registration=ad['request']['registration']
  current=admit_directed_inquiry(authority=a,registrations={registration['id']:registration},backlog=ResearchQuestionBacklog(f.store),mission=f.mission,plan=plan,inquiry=inquiry)
  new=a.resolve_for_execution(current['admission_ref']);self.assertEqual(consumer(connection,new['numeric_authority']['period_replacement_proof']),new['id'])
  from dalton_core.mission_document_research_lane import MissionDocumentResearchCoordinator,_read_holds
  from tests.test_mission_document_research_lane import _Launcher
  from dalton_core.mission_document_research_promotion import ensure_promotion_authority
  ensure_promotion_authority(connection)
  lane=MissionDocumentResearchCoordinator(store=f.store,launcher=_Launcher(f.state/'replacement-lane'),clock=f.harness.clock)
  holds={};lane._hold(holds,ad,reason='old_impossible_period',ticket_ref=None,disposition='recovery_required')
  self.assertNotIn(ad['id'],[item['id'] for item in lane._admissions()])
  self.assertEqual(lane._hold_detail(holds,[ad,new]),[])
  dispatched=lane.dispatch_once();self.assertEqual(dispatched['admission_ref'],new['id'])
  self.assertNotIn(ad['id'],_read_holds(lane.holds_path))
  current_feedback=read_document_research_feedback(f.store,f.mission)
  self.assertEqual(current_feedback['status'],'available',current_feedback)
  old_rows=[row for row in current_feedback['by_company'][ad['company_ref']] if row.get('admission_ref')==ad['id']]
  self.assertTrue(old_rows)
  self.assertEqual({row['outcome'] for row in old_rows},{'numeric_period_replacement_admitted'})
  for row in p.candidate_wire['targets']:row['figure']['period']='截至 2025-12-31'
  v.candidate_wire['candidate_ref']=candidate_ref(p.candidate_wire)
  r=h.drive(e,new);self.assertEqual(r['research_status'],'numeric_targets_completed')
  calls=(p.calls,v.calls);self.assertEqual(e.run_once(new['id']),r);self.assertEqual((p.calls,v.calls),calls)
  self.assertEqual(a.equivalent_open_admission({'company_ref':new['company_ref'],'mission_ref':new['mission_ref'],'planner_inquiry':inquiry})['admission_ref'],new['id'])
  for old in frozen:self.assertEqual(tuple(connection.execute('SELECT work_order_id,work_order_json,work_order_hash FROM scheduler_work_orders WHERE work_order_id=?',(old[0],)).fetchone()),old)
 def test_invalid_replacement_source_period_fields_or_failure_hash_refused(self):
  h,f,a,ad,e,p,v=self.old_impossible();binding=binding_for(f.store.connection,ad['id']);inquiry,plan=self.revised(h,f,a,ad,binding)
  good=derive(a,ad['mission_ref'],inquiry);self.assertIsNotNone(good)
  variants=[]
  for field,value in [('period','截至 2026-12-31'),('unit','percent'),('basis','management-reported'),('metric_ref','metric:foreign')]:
   altered=copy.deepcopy(inquiry);altered['directed_document']['numeric_targets'][0][field]=value;variants.append(altered)
  altered=copy.deepcopy(inquiry);altered['directed_document']['replaces_numeric_contract_failure']['failure_hash']='a'*64;variants.append(altered)
  altered=copy.deepcopy(inquiry);altered['question']+=' new question';variants.append(altered)
  altered=copy.deepcopy(inquiry);altered['company_ref']='company:foreign';variants.append(altered)
  for altered in variants:
   with self.assertRaises(ValueError):derive(a,ad['mission_ref'],altered)
  self.assertEqual((p.calls,v.calls),(1,0))
 def test_prior_receipt_self_hash_tamper_is_not_repeat_proof(self):
  h,(f,a,ad,e,p,v)=self.fixture();p.candidate_wire['targets'][0]['figure']['scale']='millions'
  for _ in range(8):
   r=e.run_once(ad['id'])
   if p.calls:break
  work=e.scheduler.work_order_authority(r['work_order_ref'])['work_order']
  row=f.store.connection.execute('SELECT result_envelope_id FROM scheduler_result_envelopes WHERE work_order_id=?',(work['id'],)).fetchone()
  self.assertIsNotNone(row)
  connection=f.store.connection
  class Forged:
   def execute(self,sql,args):
    cursor=connection.execute(sql,args)
    class Rows:
     def fetchall(self):
      values=[dict(v) for v in cursor.fetchall()]
      for v in values:v['content_hash']='f'*64
      return values
    return Rows()
  with self.assertRaisesRegex(ValueError,'receipt differs'):
   prior_same_failure(Forged(),work,before_attempt=2,error_type='NumericCandidateError',message="scale must be null or one of ['billion', 'million', 'thousand', 'trillion']")

 def test_old_ready_third_attempt_is_stopped_before_route_and_provider(self):
  h,(f,a,ad,e,p,v)=self.many_attempts_fixture()
  p.candidate_wire['targets'][0]['figure']['scale']='millions'
  # Simulate two formally accepted retryable replies from the old runtime.
  with patch('dalton_core.directed_numeric_failure_diagnostics.prior_same_failure',return_value=None):
   for _ in range(10):
    r=e.run_once(ad['id'])
    if p.calls==2:break
  self.assertEqual(p.calls,2)
  r=self.stop(e,ad)
  self.assertEqual((p.calls,v.calls),(2,0))
  self.assertEqual(r['reason'],'numeric_repeated_contract_failure')
  formal=e.scheduler.formal_result(r['work_order_ref'])
  self.assertEqual(formal['result_envelope']['error']['code'],'NUMERIC_REPEATED_CONTRACT_FAILURE')
  self.assertEqual(formal['attempt_number'],3)
  routes=e.draft_worker.router.list_decisions(work_order_id=r['work_order_ref'])
  self.assertEqual(len(routes),2)
  from dalton_core.document_execution_failure import read_execution_failure
  failure=read_execution_failure(f.store.connection,ad['id'])
  self.assertIn('scale must be',failure['numeric_failure_diagnostics']['diagnostic'])
  self.stop(e,ad);self.assertEqual(p.calls,2)

 def test_source_bytes_and_failure_anchor_tamper_refused_without_payment(self):
  h,f,a,ad,e,p,v=self.old_impossible()
  binding=binding_for(f.store.connection,ad['id']);inquiry,_=self.revised(h,f,a,ad,binding)
  proof=derive(a,ad['mission_ref'],inquiry)
  for key in ('formal_result_hash','result_envelope_hash','document_version_hash'):
   altered=copy.deepcopy(proof);altered['failure'][key]='f'*64
   altered['failure']['content_hash']=content_hash({k:x for k,x in altered['failure'].items() if k!='content_hash'})
   with self.assertRaises(ValueError):derive(a,ad['mission_ref'],inquiry,frozen=altered)
  raw_hash=f.source.manifest['body_sha256']
  path=f.source.spool._objects/raw_hash[:2]/raw_hash
  raw=path.read_bytes();path.write_bytes(b'source changed')
  try:
   with self.assertRaises(Exception):derive(a,ad['mission_ref'],inquiry)
  finally:path.write_bytes(raw)
  self.assertEqual(derive(a,ad['mission_ref'],inquiry),proof)
  self.assertEqual((p.calls,v.calls),(1,0))

 def test_pending_parent_has_no_period_replacement_authority(self):
  with patch('dalton_core.directed_numeric_research.assert_executable_target_periods'):
   h,(f,a,ad,e,p,v)=self.fixture(period='文件所载最近报告期末')
  self.assertIsNone(binding_for(f.store.connection,ad['id']))
  self.assertEqual((p.calls,v.calls),(0,0))

 def test_diagnostic_tail_cannot_collide_with_equal_bounded_display(self):
  from dalton_core.directed_numeric_failure_diagnostics import failure_fingerprint
  h,(f,a,ad,e,p,v)=self.fixture()
  for _ in range(8):
   r=e.run_once(ad['id'])
   if p.calls:break
  work=e.scheduler.work_order_authority(r['work_order_ref'])['work_order']
  self.assertNotEqual(failure_fingerprint(work,'NumericCandidateError','x'*1000+' first'),
   failure_fingerprint(work,'NumericCandidateError','x'*1000+' second'))

 def test_current_company_permission_removed_refuses_old_failure_replacement(self):
  from tests import test_mission_document_research as docs
  h,f,a,ad,e,p,v=self.old_impossible()
  inquiry,_=self.revised(h,f,a,ad,binding_for(f.store.connection,ad['id']))
  owner=docs.MissionDocumentResearchTests('runTest');self.addCleanup(owner.doCleanups)
  owner._roll_mission(f,universe=[{'company_ref':'company:foreign','ticker':'OTHR','coverage_tier':'A','bootstrap_priority':'P0'}])
  with self.assertRaisesRegex(Exception,'outside the active mission universe'):
   derive(a,ad['mission_ref'],inquiry)
  self.assertEqual((p.calls,v.calls),(1,0))

 def test_second_company_same_rules_and_ancestor_failure_reaches_current_plan(self):
  from tests import test_mission_annual_research as support
  from tests import test_mission_document_research as docs
  with patch.object(support,'COMPANY','synthetic-company-b'),patch.object(fixtures,'COMPANY','synthetic-company-b'),patch.object(docs,'COMPANY','synthetic-company-b'):
   h,f,a,ad,e,p,v=self.old_impossible()
   old=copy.deepcopy(f.mission)
   owner=docs.MissionDocumentResearchTests('runTest');self.addCleanup(owner.doCleanups)
   f.mission=owner._roll_mission(f,budget={**f.mission['budget'],'max_daily_cost_usd':9.0})
   self.assertNotEqual(f.mission['id'],old['id'])
   from dalton_core.document_research_feedback import read_document_research_feedback
   feedback=read_document_research_feedback(f.store,f.mission)
   self.assertEqual(feedback['status'],'available',feedback)
   failure=next(v for v in feedback['by_company']['synthetic-company-b'] if v.get('replaces_numeric_contract_failure'))
   self.assertEqual(failure['mission_version_ref'],old['id'])
   self.assertEqual(failure['governing_mission_version_ref'],f.mission['id'])
   binding=failure['replaces_numeric_contract_failure'];inquiry,plan=self.revised(h,f,a,ad,binding)
   registration=ad['request']['registration']
   admitted=admit_directed_inquiry(authority=a,registrations={registration['id']:registration},backlog=ResearchQuestionBacklog(f.store),mission=f.mission,plan=plan,inquiry=inquiry)
   current=a.resolve_for_execution(admitted['admission_ref'])
   self.assertEqual(current['company_ref'],'synthetic-company-b')
   self.assertEqual((p.calls,v.calls),(1,0))
   from dalton_core.mission_document_research import MissionDocumentResearchEquivalentPending
   another=copy.deepcopy(inquiry);another['because']+=' Same question, a later day.'
   from dalton_core.mission_document_research import _exact_plan
   another_plan=copy.deepcopy(_exact_plan(f.store.connection,plan['plan_id'])[0]);another_plan['inquiries']=[another];another_plan['state_hash']='5'*64
   # _record_plan accepts the planner body, not stored table-only fields.
   another_plan={k:another_plan[k] for k in ('schema_version','task_ref','created_at','state_hash','mission_version_ref','assessment','directives','inquiries','sufficiency')}
   another_plan['content_hash']=content_hash(another_plan);another_plan=h._record_plan(f,another_plan)
   with self.assertRaises(MissionDocumentResearchEquivalentPending):
    admit_directed_inquiry(authority=a,registrations={registration['id']:registration},backlog=ResearchQuestionBacklog(f.store),mission=f.mission,plan=another_plan,inquiry=another)
   self.assertEqual((p.calls,v.calls),(1,0))
