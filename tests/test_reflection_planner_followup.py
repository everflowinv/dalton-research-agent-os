import copy,json,sqlite3,tempfile,unittest
from pathlib import Path
from dalton_core.store import DaltonStore,content_hash,canonical_json
from dalton_core.coverage_mission import CoverageMissionAuthority
from dalton_core.research_followup import ResearchFollowupAuthority,requests
from dalton_core.research_followup_automation import FollowupAutomationAuthority,planner_context,latest,groups
from dalton_core.research_cycle_reflection import backlog_candidates
from dalton_core.research_state import state_content_hash,planning_core_hash
from dalton_core.research_planner import build_prompt,plan_from_response,project_state_for_prompt
from dalton_core.research_planner_cli import build_state
from tests.p9a_fixtures import bootstrap_method_authorities,mission_params

class ReflectionPlannerConsumptionTests(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
  self.store=DaltonStore(str(self.root/'core.sqlite'));self.addCleanup(self.store.close);self.c=self.store.connection
  self.missions=CoverageMissionAuthority(self.store);params=mission_params(bootstrap_method_authorities(self.store));ref=params.pop('mission_ref')
  params['autonomy']['may_write']=sorted(set(params['autonomy']['may_write']+['research_task','model_run','stage_record']))
  self.mission=self.missions.create_mission(ref,**params);self.followup=ResearchFollowupAuthority(self.store);self.auto=FollowupAutomationAuthority(self.store)
  self.c.execute('CREATE TABLE research_cycle_reflection_versions(version_id TEXT,record_json TEXT,content_hash TEXT)')
  metrics={'spend':{'available':False},'backlog':{'available':True,'new_questions':8,'answered':1,'new_question_refs':['research-question:synthetic:1']},'planner':{'available':True,'inquiries_raised':8,'dispatched':0,'research_task_granted':True,'probe_templates':2},'human_checkpoints':{'available':False},'ticks':{'available':False},'quality_scores':{'available':False},'journal':{'available':False}}
  body={'id':'reflection:synthetic:1','mission_ref':ref,'created_at':'2026-10-01','metrics':metrics,'window':{'iso_week':'2026-W40'},'backlog_candidates':backlog_candidates(metrics,{'iso_week':'2026-W40'})};body['content_hash']=content_hash(body)
  self.c.execute('INSERT INTO research_cycle_reflection_versions VALUES(?,?,?)',(body['id'],canonical_json(body),body['content_hash']))
  self.followup.capture_existing([body['id']]);self.context=planner_context(self.c,self.mission)
  self.state=build_state(self.store,self.missions,self.mission,plans_dir=self.root/'plans',as_of='2026-10-10T12:00:00+00:00',document_inventory={})
  self.text=canonical_json({'schema_version':'0.1','assessment':'没有新增安排：现有已登记工作先按当前预算执行，排序建议仍保留。','directives':[],'inquiries':[],'sufficiency':[]})
  self.prompt=build_prompt(self.state);self.plan=plan_from_response(self.state,self.text,created_at='2026-10-10T12:00:00+00:00')
  self.stored=self.missions.record_research_plan(self.plan,decided_by=self.mission['autonomy']['automation_principal'],work_order_ref='work:synthetic-plan')
  self.s=sqlite3.connect(self.root/'scheduler.sqlite');self.addCleanup(self.s.close)
  self.s.executescript('CREATE TABLE scheduler_work_orders(work_order_id TEXT,work_order_json TEXT,work_order_hash TEXT);CREATE TABLE scheduler_result_envelopes(result_envelope_id TEXT,work_order_id TEXT,result_envelope_json TEXT,result_envelope_hash TEXT,outcome TEXT);CREATE TABLE scheduler_formal_results(work_order_id TEXT,result_envelope_id TEXT,result_envelope_json TEXT,terminal_state TEXT);')
  self.work={'id':'work:synthetic-plan','question':self.prompt,'metadata':{'purpose':'plan'}}
  self.result={'id':'result:synthetic-plan','work_order_ref':self.work['id'],'status':'succeeded','outputs':{'text':self.text}}
  self.s.execute('INSERT INTO scheduler_work_orders VALUES(?,?,?)',(self.work['id'],canonical_json(self.work),content_hash(self.work)))
  self.s.execute('INSERT INTO scheduler_result_envelopes VALUES(?,?,?,?,?)',(self.result['id'],self.work['id'],canonical_json(self.result),content_hash(self.result),'succeeded'))
  self.s.execute('INSERT INTO scheduler_formal_results VALUES(?,?,?,?)',(self.work['id'],self.result['id'],canonical_json(self.result),'succeeded'));self.s.commit()
  self.call={'work_order_ref':self.work['id'],'result_envelope_ref':self.result['id'],'text':self.text}
 def observe(self):return self.auto.observe_planner(context=self.context,full_state=self.state,stored_plan=self.stored,call=self.call,prompt=self.prompt)
 def test_actual_normal_plan_consumes_frozen_suggestions_without_fabricating_inquiry_or_completion(self):
  original=list(self.c.execute('SELECT record_json,content_hash FROM research_followup_requests'));budget=copy.deepcopy(self.mission['budget'])
  self.assertEqual(self.state['reflection_followup_context'],self.context)
  out=self.observe();self.assertEqual(len(out),2)
  self.assertTrue(all(x['state']=='waiting_evidence' for x in out));self.assertTrue(all(x['evidence']['inquiries']==0 for x in out))
  self.assertTrue(all(x['evidence']['plan_ref']==self.stored['plan_id'] for x in out))
  self.assertEqual(original,list(self.c.execute('SELECT record_json,content_hash FROM research_followup_requests')))
  self.assertEqual(self.missions.mission(self.mission['id'])['budget'],budget)
 def test_same_source_wakes_once_and_actual_lane_status_does_not_repay_or_reappend_adoption(self):
  before=planning_core_hash({k:v for k,v in self.state.items() if k!='reflection_followup_context'})
  self.assertNotEqual(before,planning_core_hash(self.state));first=self.observe()
  self.auto.observe_lane('dispatch_research_task',{'status':'idle','reason':'current plan has no directed document inquiry'})
  n=self.c.execute('SELECT count(*) FROM research_followup_automation_versions').fetchone()[0]
  self.assertEqual(self.auto.observe_planner(context=self.context,full_state=self.state,stored_plan=self.stored),first)
  self.assertEqual(self.c.execute('SELECT count(*) FROM research_followup_automation_versions').fetchone()[0],n)
  later={**self.state,'as_of':'2026-10-11T12:00:00+00:00','spend':{'cost_usd':999}}
  self.assertEqual(state_content_hash(later),state_content_hash(self.state));self.assertEqual(planner_context(self.c,self.mission),self.context)
 def test_prompt_projection_preserves_exact_source_hash_and_grant(self):
  projected=project_state_for_prompt(self.state,max_input_bytes=64000)
  self.assertEqual(projected['reflection_followup_context'],self.context)
  prompt=build_prompt(projected)
  for group in self.context['groups']:
   for suggestion in group['suggestions']:self.assertIn(suggestion['reflection_hash'],prompt)
 def test_fake_result_prompt_or_plan_hash_never_records_consumption(self):
  for field,value in [('text','{}'),('work_order_ref','work:unbound'),('result_envelope_ref','result:unbound')]:
   with self.subTest(field=field),self.assertRaises((ValueError,TypeError)):
    self.auto.observe_planner(context=self.context,full_state=self.state,stored_plan=self.stored,call={**self.call,field:value},prompt=self.prompt)
  with self.assertRaises((ValueError,KeyError)):
   self.auto.observe_planner(context=self.context,full_state=self.state,stored_plan=self.stored,call=self.call,prompt=self.prompt.replace('reflection_followup_context','wrong_context'))
  self.assertEqual(self.c.execute('SELECT count(*) FROM research_followup_automation_versions').fetchone()[0],0)
 def test_human_instruction_and_foreign_mission_do_not_enter_automatic_brain_context(self):
  self.assertIsNone(planner_context(self.c,{**self.mission,'id':'mission:foreign'}))
  self.followup.decide(requests(self.c)[0]['id'],action='assign',actor_ref='human:owner',reason='I will judge this exact issue.',owner_ref='human:analyst',due_on='2030-01-01')
  ctx=planner_context(self.c,self.mission);self.assertEqual(len(ctx['groups']),1)

 def test_outer_quiet_signature_sees_source_requests_but_not_automatic_observation_ticks(self):
  from dalton_core.research_planner_launcher import ResearchPlannerCoordinator
  lane=ResearchPlannerCoordinator(store=self.store,launcher=None)
  signature=lane._signature();self.assertEqual(signature['reflection_requests'],2)
  self.observe();self.assertEqual(lane._signature(),signature)
  self.auto.observe_lane('dispatch_research_task',{'status':'idle','reason':'no directed document inquiry'})
  self.assertEqual(lane._signature(),signature)
  self.followup.decide(requests(self.c)[0]['id'],action='assign',actor_ref='human:owner',reason='I will judge.',owner_ref='human:analyst',due_on='2030-01-01')
  self.assertNotEqual(lane._signature(),signature)

 def test_actual_compressed_prompt_does_not_replace_full_directed_reading_admission_state(self):
  from dalton_core.research_planner import ResearchPlanError
  company=self.state['companies'][0]
  document={'document_ref':'sales-note:synthetic-directed','document_version_hash':'a'*64,
            'authority_ref':'registered-document:sha256:'+'a'*64,'authority_hash':'a'*64,
            'source_ref':'source:sales-notes','readable':True,
            'operations':['search_registered_document','read_registered_document']}
  company['readable_documents']=[document]
  self.state['document_research_policy']={'max_query_terms':10,'max_query_term_chars':160}
  self.state['content_hash']=state_content_hash(self.state)
  from dalton_core.document_research_strategy import STRATEGY_VERSION
  strategy={'strategy_version':STRATEGY_VERSION,'document_ref':document['document_ref'],
            'document_version_hash':document['document_version_hash'],'query_terms':['revenue'],
            'query_rationale':'核对已登记原文。'}
  self.text=canonical_json({'schema_version':'0.1','assessment':'按现有授权读取原文。','directives':[],
      'inquiries':[{'company_ref':company['company_ref'],'question':'原文怎样解释收入变化？',
                    'wants':'来源绑定的原文依据','because':'已有材料可核验。','directed_document':strategy}],
      'sufficiency':[]})
  projected=copy.deepcopy(project_state_for_prompt(self.state,max_input_bytes=64000))
  for entry in projected['companies']:
   for doc in entry.get('readable_documents',[]):
    for key in ('authority_ref','authority_hash','operations'):doc.pop(key,None)
  self.prompt=build_prompt(projected)
  with self.assertRaisesRegex(ResearchPlanError,'verified directed-reading capability'):
   plan_from_response(projected,self.text,created_at=self.plan['created_at'])
  self.plan=plan_from_response(self.state,self.text,created_at=self.plan['created_at'])
  self.stored=self.missions.record_research_plan(self.plan,decided_by=self.mission['autonomy']['automation_principal'],work_order_ref=self.work['id'])
  self.work['question']=self.prompt;self.result['outputs']['text']=self.text;self.call['text']=self.text
  self.s.execute('UPDATE scheduler_work_orders SET work_order_json=?,work_order_hash=?',(canonical_json(self.work),content_hash(self.work)))
  self.s.execute('UPDATE scheduler_result_envelopes SET result_envelope_json=?,result_envelope_hash=?',(canonical_json(self.result),content_hash(self.result)))
  self.s.execute('UPDATE scheduler_formal_results SET result_envelope_json=?',(canonical_json(self.result),));self.s.commit()
  observed=self.observe()
  self.assertEqual(len(observed),2)
  self.assertTrue(all(x['state']=='waiting_evidence' and x['evidence']['inquiries']==1 for x in observed))
  self.assertEqual(self.auto.observe_planner(context=self.context,full_state=self.state,stored_plan=self.stored),observed)

 def test_wrong_full_state_content_declared_hash_or_context_refuses_without_ledger(self):
  for mutation in ('content','declared','context'):
   full=copy.deepcopy(self.state)
   if mutation=='content':full['companies'][0]['ticker']='Changed source input'
   elif mutation=='declared':full['content_hash']='f'*64
   else:full['reflection_followup_context']=None
   with self.subTest(mutation=mutation),self.assertRaisesRegex(ValueError,'full planner state'):
    self.auto.observe_planner(context=self.context,full_state=full,stored_plan=self.stored,call=self.call,prompt=self.prompt)
  self.assertEqual(self.c.execute('SELECT count(*) FROM research_followup_automation_versions').fetchone()[0],0)
