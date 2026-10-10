"""New proposals cannot leave economic mechanisms as an unexecuted list."""
import copy,json,unittest
from unittest.mock import patch
from dalton_core.company_model_spec import spec_from_response,build_prompt,CompanyModelSpecError,OUTPUT_SCHEMA,TASK_HASH
from dalton_core.company_model_cli import choose_company
from tests.test_company_model_spec import STATE,_spec as response,DECIDED_BY

class DriverCompletenessTests(unittest.TestCase):
 def body(self):return response()
 def parse(self,body):return spec_from_response(STATE,json.dumps(body),decided_by=DECIDED_BY)
 def test_producer_requires_plan_and_clear_source_gap_instructions(self):
  self.assertIn('driver_formulas',OUTPUT_SCHEMA['required']);prompt=build_prompt(STATE);self.assertIn('Never fabricate a member',prompt);self.assertIn('exact missing material',prompt)
 def test_missing_plan_rejected_with_concrete_driver_refs(self):
  b=self.body();b.pop('driver_formulas')
  with self.assertRaisesRegex(CompanyModelSpecError,'driver_formulas.*billable-headcount.*realised-rate'):self.parse(b)
 def test_partial_and_empty_plans_do_not_pass(self):
  for plan in ([],[{'output_ref':'revenue','operator':'sum','driver_refs':['realised-rate'],'because':'only price'}]):
   b=self.body();b['driver_formulas']=plan
   with self.subTest(plan=plan),self.assertRaisesRegex(CompanyModelSpecError,'missing driver refs:.*billable-headcount'):self.parse(b)
 def test_complete_plan_preserved_without_inventing_missing_history(self):
  b=self.body();s=self.parse(b);self.assertEqual(s['driver_formulas'],b['driver_formulas']);self.assertIsNone(s['revenue_drivers'][0]['basis_concept'])
 def test_unknown_or_duplicate_inputs_still_refused(self):
  for refs in (['invented','realised-rate'],['billable-headcount','billable-headcount']):
   b=self.body();b['driver_formulas'][0]['driver_refs']=refs
   with self.subTest(refs=refs),self.assertRaisesRegex(CompanyModelSpecError,'unknown or duplicate'):self.parse(b)
 def test_legacy_single_mix_identity_is_compatible(self):
  b=self.body();b.pop('driver_formulas');b['revenue_drivers']=[{**b['revenue_drivers'][1],'ref':'mix','kind':'mix','basis_concept':b['revenue_anchor_concept']}]
  for item in b['expense_lines']:item['driver_ref']=None
  s=self.parse(b);self.assertNotIn('driver_formulas',s)
 def test_normal_selector_requests_new_task_instead_of_replaying_old_missing_plan(self):
  old='7e59bb11f19aab6252ef4b46ae4496890b7a4149464eaad69c05db2bce4355fa';self.assertNotEqual(TASK_HASH,old)
  class Port:
   def statement_filings(self):return [{'company_ref':STATE['company_ref']}]
   def company_model_spec_for_state(self,company,state_hash,*,task_hash=None):self.called=task_hash;return {'legacy':'held'} if task_hash==old else None
  port=Port()
  with patch('dalton_core.company_model_cli.build_company_model_state',return_value=STATE):
   company,state=choose_company(port,{'universe':[{'company_ref':STATE['company_ref'],'ticker':'SYN'}]})
  self.assertEqual(company,STATE['company_ref']);self.assertEqual(port.called,TASK_HASH)
