"""Producer instructions describe the existing strict statement contract."""
import unittest
from dalton_core.company_model_spec import build_prompt,OUTPUT_SCHEMA
from dalton_core.company_financial_statement_structure import _normalize_statement_leaf,FinancialStatementStructureError,_STRUCTURE_LINE_SCHEMA
from tests.test_company_model_spec import STATE

class ProducerBalanceGuidanceTests(unittest.TestCase):
 def test_prompt_and_schema_distinguish_balance_instants_from_recurring_income(self):
  prompt=build_prompt(STATE);desc=_STRUCTURE_LINE_SCHEMA['properties']['forecast_method']['description']
  self.assertIn('A balance-sheet leaf never uses carry_forward_mean',prompt)
  self.assertIn('never use carry_forward_mean',desc)
  for text in [prompt,desc]:
   for token in ['accounts_receivable','accounts_payable','deferred_revenue','hold_last','share_of_line','cash_and_equivalents','not_applicable']:self.assertIn(token,text)
 def test_prompt_requires_real_labels_and_explicit_nullable_fields(self):
  self.assertIn('nonempty source-grounded label',build_prompt(STATE))
  self.assertIn('annual_forecast_method is null for all non-share lines',build_prompt(STATE))
  self.assertIn('label',_STRUCTURE_LINE_SCHEMA['required']);self.assertFalse(_STRUCTURE_LINE_SCHEMA['additionalProperties'])
 def filed(self,statement='balance',kind='instant'):
  return {'concept:stock':{'status':'filed','statement':statement,'period_basis':kind,'cells':{'2026-06-30':{'unit':'usd','value':'10'}}}}
 def leaf(self,role,method,base=None):
  return {'role':role,'statement':'balance','period_kind':'instant','annual_semantics':'not_applicable','kind':'filed','forecast_method':method,'forecast_base_ref':base,'annual_forecast_method':None,'concept':'concept:stock','unit':'usd'}
 def test_advertised_stock_methods_are_accepted_but_means_remain_refused(self):
  for role in ['accounts_receivable','accounts_payable','deferred_revenue','debt_current','debt_noncurrent','depreciable_assets','amortizable_assets']:
   for method,base in [('hold_last',None),('share_of_line','revenue'),('unavailable',None)]:
    with self.subTest(role=role,method=method):_normalize_statement_leaf(self.leaf(role,method,base),0,self.filed())
   for method in ['carry_forward_mean','quarterly_growth','roll_forward']:
    with self.subTest(role=role,method=method),self.assertRaises(FinancialStatementStructureError):_normalize_statement_leaf(self.leaf(role,method),0,self.filed())
 def test_cash_stock_roll_forward_is_allowed_but_cash_duration_keeps_its_own_semantics(self):
  _normalize_statement_leaf(self.leaf('cash_and_equivalents','roll_forward'),0,self.filed())
  with self.assertRaises(FinancialStatementStructureError):_normalize_statement_leaf(self.leaf('cash_and_equivalents','carry_forward_mean'),0,self.filed())
  line={'role':'share_repurchase','statement':'cash','period_kind':'duration','annual_semantics':'sum_quarters','kind':'filed','forecast_method':'carry_forward_mean','forecast_base_ref':None,'annual_forecast_method':None,'concept':'concept:stock','unit':'usd'};_normalize_statement_leaf(line,0,self.filed('cash','duration'))

if __name__=='__main__':unittest.main()
