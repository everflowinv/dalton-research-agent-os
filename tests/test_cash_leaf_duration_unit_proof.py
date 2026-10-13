"""Cumulative filed facts can prove currency without inventing quarterly amounts."""
import copy
import unittest
from dalton_core import company_financial_statement_structure as structure
from dalton_core.company_model_cli import model_spec_validation_contract
from dalton_core.forecast_coverage import complete_structure_definition, filed_values
from tests import test_forecast_coverage as fixture

class CashLeafDurationUnitTests(unittest.TestCase):
    def setUp(self):
        self.data=fixture.load()
        complete=complete_structure_definition(self.data['spec']['financial_statement_structure'],
            company_spec=self.data['spec'],financial_inputs=self.data['table'])
        self.data['spec']['financial_statement_structure']={k:complete[k]for k in ('schema_version','lines','formulas')}
        self.leaf=next(l for l in complete['lines']if l['role']=='dividends_paid')
        self.source=next(l for l in self.data['table']['filed_lines']if l['concept']==self.leaf['concept'])
        self.source['cells']={}
        self.source['duration_facts']=[{'period_start':'2025-01-01','period_end':'2025-06-30',
            'period_kind':'cumulative','value':'4977000000','unit':'USD','source_accessions':['0000000001-26-000071']}]

    def test_full_materialization_accepts_exact_duration_unit_without_making_quarters(self):
        before=copy.deepcopy(self.data['table'])
        result,replay=structure.materialize_financial_statement_structure(
            self.data['spec'],self.data['table'],complete=False)
        leaf=next(l for l in result['lines']if l['role']=='dividends_paid')
        self.assertEqual(leaf['unit'],'usd')
        self.assertEqual(self.data['table'],before)
        self.assertFalse(self.source['cells'])
        self.assertEqual(filed_values(self.source),{})
        self.assertTrue(replay)

    def test_conflicting_duration_currency_remains_rejected(self):
        self.source['duration_facts'].append({**self.source['duration_facts'][0],'unit':'EUR'})
        with self.assertRaisesRegex(structure.FinancialStatementStructureError,'unit differs'):
            structure.materialize_financial_statement_structure(self.data['spec'],self.data['table'],complete=False)

    def test_wrong_declared_currency_or_no_unit_proof_remains_rejected(self):
        for units in (['EUR'],[]):
            with self.subTest(units=units):
                self.source['duration_facts']=[{**self.source['duration_facts'][0],'unit':units[0]}]if units else []
                with self.assertRaisesRegex(structure.FinancialStatementStructureError,'unit differs'):
                    structure.materialize_financial_statement_structure(self.data['spec'],self.data['table'],complete=False)

    def test_old_non_typed_unit_rule_is_unchanged(self):
        with self.assertRaisesRegex(structure.FinancialStatementStructureError,'unit differs'):
            structure._normalize_statement_leaf(copy.deepcopy(self.leaf),0,{self.source['concept']:self.source},
                schema_version=structure.LEGACY_SCHEMA_VERSION)

    def test_validation_identity_wakes_normal_revalidation_without_new_task(self):
        self.assertEqual(model_spec_validation_contract()['cash_leaf_duration_unit'],
                         structure.CASH_LEAF_DURATION_UNIT_VALIDATION_REF)

if __name__=='__main__':unittest.main()
