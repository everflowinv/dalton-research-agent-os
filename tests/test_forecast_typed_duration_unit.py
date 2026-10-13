"""Cumulative unit proof is consumed without manufacturing quarterly history."""
import copy
import unittest
from unittest.mock import patch
from dalton_core import company_financial_statement_structure as structure
from dalton_core.company_model_forecast import model_digest
from dalton_core.forecast_coverage import complete_structure_definition
from dalton_core.model_forecast_driver import (
    ForecastModelUnavailable, build_structure_drivers,
    build_structured_forecast_model,
)
from tests import test_forecast_coverage as fixture

class TypedDurationUnitTests(unittest.TestCase):
    def setUp(self):
        self.data=fixture.load()
        full=complete_structure_definition(self.data['spec']['financial_statement_structure'],
            company_spec=self.data['spec'],financial_inputs=self.data['table'])
        self.data['spec']['financial_statement_structure']={k:full[k]for k in ('schema_version','lines','formulas')}
        leaf=next(l for l in full['lines']if l['role']=='dividends_paid')
        self.source=next(l for l in self.data['table']['filed_lines']if l['concept']==leaf['concept'])
        self.source['cells']={}
        self.source['duration_facts']=[{'period_start':'2025-01-01','period_end':'2025-06-30',
            'period_kind':'cumulative','value':'4977000000','unit':'USD','source_accessions':['0000000001-26-000071']}]

    def materialize(self):
        return structure.materialize_financial_statement_structure(self.data['spec'],self.data['table'],complete=False)

    def test_same_typed_unit_proof_keeps_quarter_history_empty(self):
        before=copy.deepcopy(self.data['table']);materialized,replay=self.materialize()
        drivers=build_structure_drivers(self.data['table'],materialized)
        dividend=next(d for d in drivers if d['role']=='dividends_paid')
        self.assertEqual(dividend['unit'],'usd');self.assertEqual(dividend['history'],[])
        self.assertEqual(self.data['table'],before)
        self.assertTrue(replay)

    def test_full_forecast_does_not_use_half_year_dividend_as_quarter(self):
        materialized,replay=self.materialize()
        binding=structure.forecast_structure_binding(materialized,replay,self.data['table'])
        body=build_structured_forecast_model(self.data['spec'],self.data['table'],
            structure=materialized,replay=replay,binding=binding,actor_ref='automation:test')
        dividend=next(d for d in body['drivers']if d['role']=='dividends_paid')
        self.assertEqual(dividend['history'],[])
        results=[r for r in body['results']if r['role']=='dividends_paid']
        self.assertTrue(results)
        self.assertTrue(all(c['value'] is None for r in results for c in r['cells']))

    def test_wrong_or_mixed_currency_and_missing_proof_remain_refused(self):
        materialized,_=self.materialize()
        for units in (['EUR'],['USD','EUR'],[]):
            with self.subTest(units=units):
                self.source['duration_facts']=[{**self.source['duration_facts'][0],'unit':unit}for unit in units]if units else []
                with self.assertRaisesRegex(ForecastModelUnavailable,'unit differs'):
                    build_structure_drivers(self.data['table'],materialized)

    def test_legacy_structure_does_not_gain_cumulative_unit_authority(self):
        materialized,_=self.materialize();materialized['schema_version']=structure.LEGACY_SCHEMA_VERSION
        with self.assertRaisesRegex(ForecastModelUnavailable,'unit differs'):
            build_structure_drivers(self.data['table'],materialized)

    def test_typed_run_identity_changes_when_shared_unit_rule_changes(self):
        current=model_digest(self.data['spec'],self.data['table'])
        with patch.object(structure,'CASH_LEAF_DURATION_UNIT_VALIDATION_REF','filed-cash-leaf-duration-unit-proof:before'):
            prior=model_digest(self.data['spec'],self.data['table'])
        self.assertNotEqual(current,prior)

if __name__=='__main__':unittest.main()
