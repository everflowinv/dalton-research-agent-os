from __future__ import annotations
import copy,json,unittest
from dalton_core.planner_numeric_requirements import numeric_requirements
from dalton_core.research_planner import ResearchPlanError,build_prompt,plan_from_response,project_state_for_prompt
from dalton_core.research_state import company_state,planning_core_hash
from dalton_core.store import content_hash
from tests import test_research_planner as f

class NumericRequirementTests(unittest.TestCase):
 def setUp(self):
  self.question='What are the filed obligation amount and recognition duration?'
  self.binding={'admission_ref':'mission-document-research-admission:synthetic','admission_hash':'a'*64,'rejection_ref':'mission-document-research-candidate-rejection:synthetic','rejection_hash':'b'*64}
  self.doc={'document_ref':'sec:filing:synthetic','document_version_hash':'c'*64,'authority_hash':'c'*64,'authority_ref':'registered-document:synthetic','readable':True,'completeness':'complete','operations':['search_registered_document']}
  self.obs={'id':'observation:synthetic','created_at':f.NOW,'question':self.question,'document_ref':self.doc['document_ref'],'document_authority_hash':'c'*64,'numeric_strategy_required':True,'replaces_numeric_rejection':self.binding}
  self.state=f.state(readable_documents_by_company={f.ACN:[self.doc]},document_research_feedback={"status":"available","by_company":{f.ACN:[self.obs]}},document_research_policy={'max_query_terms':16,'max_query_term_chars':240})
 def reply(self,question=None,*,numeric=False,binding=None,company=f.ACN):
  s={'strategy_version':'directed-document-numeric:0.1' if numeric else 'directed-document:0.1','document_ref':self.doc['document_ref'],'document_version_hash':'c'*64,'query_terms':['remaining performance obligations'],'query_rationale':'Read the filed note.'}
  if numeric:s['numeric_targets']=[{'target_ref':'target:synthetic-obligation','metric_ref':'metric:synthetic-obligation','metric_definition':{'label':'Obligation','meaning':'Filed remaining contractual obligation.'},'period':'FY2025','unit':'currency','currency':'USD','basis':'gaap-reported'}]
  if binding is not None:s['replaces_numeric_rejection']=binding
  return f.response(inquiries=[f.inquiry(company_ref=company,question=question or self.question,directed_document=s)])
 def plan(self,reply,state=None):return plan_from_response(state or self.state,reply,created_at=f.NOW)
 def test_same_exact_question_cannot_downgrade_or_drop_required_binding(self):
  with self.assertRaisesRegex(ResearchPlanError,'cannot use a qualitative'):self.plan(self.reply())
  with self.assertRaisesRegex(ResearchPlanError,'exact rejection binding'):self.plan(self.reply(numeric=True))
  self.assertEqual(self.plan(self.reply(numeric=True,binding=self.binding))['inquiries'][0]['directed_document']['replaces_numeric_rejection'],self.binding)
 def test_explicit_bound_followup_requires_numeric_and_rejects_old_hash(self):
  with self.assertRaisesRegex(ResearchPlanError,'cannot use a qualitative'):self.plan(self.reply('A follow-up phrasing',binding=self.binding))
  changed={**self.binding,'rejection_hash':'d'*64}
  with self.assertRaisesRegex(ResearchPlanError,'not exact'):self.plan(self.reply(numeric=True,binding=changed))
  self.plan(self.reply('A follow-up phrasing',numeric=True,binding=self.binding))
 def test_historical_binding_without_known_feedback_still_uses_original_admission_gate(self):
  legacy=copy.deepcopy(self.state)
  for company in legacy['companies']:
   company.pop('numeric_reading_requirements',None)
   company['document_research_feedback']=[]
  self.plan(self.reply(numeric=True,binding=self.binding),legacy)
 def test_long_question_excerpt_keeps_full_question_identity(self):
  full=self.question+' '+('original scope '*100)
  requirement=numeric_requirements([{**self.obs,'question':full}])['requirements'][0]
  self.assertEqual(len(requirement['question']),600)
  self.assertTrue(requirement['question_excerpted'])
  self.assertEqual(requirement['question_hash'],content_hash(full.strip()))
 def test_other_qualitative_question_on_same_document_remains_valid(self):
  self.plan(self.reply('What accounting policy explains how obligations are recognized?'))
  # No fuzzy matching of paraphrases to another question's authority.
  self.plan(self.reply('How large are the obligations?'))
 def test_foreign_original_and_company_binding_are_refused(self):
  foreign=copy.deepcopy(self.state);foreign['companies'][0]['readable_documents'][0]['document_version_hash']='d'*64
  reply=json.loads(self.reply(numeric=True,binding=self.binding));reply['inquiries'][0]['directed_document']['document_version_hash']='d'*64
  with self.assertRaisesRegex(ResearchPlanError,'another original version'):self.plan(json.dumps(reply),foreign)
  with self.assertRaisesRegex(ResearchPlanError,'not exact'):self.plan(self.reply(numeric=True,binding=self.binding,company=f.IBM))
 def test_bounded_deduplicated_requirements_do_not_invent_numeric_flag(self):
  raw=[self.obs]*5
  self.assertEqual(len(numeric_requirements(raw)['requirements']),1)
  self.assertEqual(numeric_requirements([{**self.obs,'numeric_strategy_required':False}])['requirements'],[])
  observations=[]
  for n in range(6):
   observations.append({**self.obs,'id':f'obs:{n}','replaces_numeric_rejection':{**self.binding,'rejection_ref':f'rejection:{n}'}})
  result=numeric_requirements(observations)
  self.assertEqual(len(result['requirements']),4);self.assertEqual(result['omitted'],2);self.assertTrue(result['omitted_hash'])
 def test_new_requirement_wakes_once_but_preview_and_feedback_count_noise_do_not(self):
  old=f.state();self.assertNotEqual(planning_core_hash(old),planning_core_hash(self.state))
  repeated=f.state(readable_documents_by_company={f.ACN:[self.doc]},document_research_feedback={"status":"available","by_company":{f.ACN:[self.obs]*3}},document_research_policy={'max_query_terms':16,'max_query_term_chars':240})
  self.assertEqual(planning_core_hash(self.state),planning_core_hash(repeated))
 def test_64kb_zero_feedback_projection_keeps_requirement_and_exact_old_original(self):
  helper=f.FeedbackProjectionTests();built=helper.worst_case(4)
  for company in built['companies']:
   company['document_research_feedback']=[{**self.obs,'company_ref':company['company_ref']},*company['document_research_feedback']]
   company['numeric_reading_requirements']=numeric_requirements(company['document_research_feedback'])
   company['readable_documents'].append({**self.doc,'doc_date':'2020-01-01'})
  built=helper.rebind(built);projected=project_state_for_prompt(built,max_input_bytes=64000)
  self.assertEqual(projected['prompt_projection']['document_research_feedback_keep_per_company'],0)
  self.assertEqual(len(projected['companies']),4)
  self.assertLessEqual(len(build_prompt(projected).encode()),64000)
  for company in projected['companies']:
   self.assertEqual(company['numeric_reading_requirements']['requirements'][0]['replaces_numeric_rejection'],self.binding)
   self.assertIn(self.doc['document_ref'],[d['document_ref'] for d in company['readable_documents']])

if __name__=='__main__':unittest.main()
