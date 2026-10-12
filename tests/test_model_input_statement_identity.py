"""Declared statement identity cannot mix cash and income values or hide ambiguity."""
from copy import deepcopy
import unittest
from unittest.mock import patch
from tests import test_company_model_inputs as fixtures
from dalton_core.company_model_inputs import (
    AMBIGUOUS, FILED, _series_for, _declared_statement_hints,
    build_model_inputs, STATEMENT_IDENTITY_RULE_REF,
)
from dalton_core.company_financial_statement_structure import materialize_financial_statement_structure, FinancialStatementStructureError
from dalton_core.store import canonical_json

CONCEPT = 'synthetic:RecognizedExpense'


def structure(statement='income'):
    return {'schema_version':'0.6','lines':[{
        'ref':'expense','kind':'filed','concept':CONCEPT,'statement':statement}], 'formulas':[]}


def source(value, statement='income', **extra):
    return fixtures._line(CONCEPT,'2026-04-01','2026-06-30',value,statement=statement) | {
        'line_id':'line:'+statement,'ingest_id':'ingest:synthetic','ordinal':1,
        'dimensions':{},'statement_context_ref':'context:synthetic',**extra}


class StatementSourceIdentityTests(unittest.TestCase):
    def test_income_and_cash_values_are_separately_preserved(self):
        ledger=fixtures.FakeMissions([source('11'),source('73','cash')])
        income=_series_for(ledger,'company:ticker:other',CONCEPT,statement_hint='income')
        cash=_series_for(ledger,'company:ticker:other',CONCEPT,statement_hint='cash')
        self.assertEqual(income['series']['quarters'][0]['value'],'11')
        self.assertEqual(cash['series']['quarters'][0]['value'],'73')
        for result,statement in ((income,'income'),(cash,'cash')):
            self.assertEqual(result['statement_scope']['rule_ref'],STATEMENT_IDENTITY_RULE_REF)
            self.assertEqual({r['statement']for r in result['source_evidence']['rows']},{statement})
            self.assertEqual(result['source_evidence']['rows'][0]['statement_context_ref'],'context:synthetic')
        self.assertEqual(_series_for(ledger,'company:ticker:other',CONCEPT)['status'],AMBIGUOUS)

    def test_declared_scope_controls_table_not_the_label_or_larger_value(self):
        ledger=fixtures.FakeMissions([source('11',label='Tax'),source('999999','cash',label='Expense')])
        spec=fixtures._spec(drivers=[],expenses=[]);spec['financial_statement_structure']=structure()
        table=build_model_inputs(ledger,spec)
        self.assertEqual(table['filed_lines'][0]['status'],FILED)
        self.assertEqual(table['filed_lines'][0]['cells']['2026-06-30']['value'],'11')
        self.assertEqual(table['statement_identity_rule_ref'],STATEMENT_IDENTITY_RULE_REF)
        self.assertEqual(table['filed_lines'][0]['source_evidence']['rows'][0]['line_id'],'line:income')

    def test_conflicting_declared_statements_remain_ambiguous(self):
        spec={'financial_statement_structure':structure()}
        spec['financial_statement_structure']['lines'].append({'ref':'cash-expense','kind':'filed','concept':CONCEPT,'statement':'cash'})
        self.assertNotIn(CONCEPT,_declared_statement_hints(spec))
        spec.update(fixtures._spec(drivers=[],expenses=[]))
        table=build_model_inputs(fixtures.FakeMissions([source('11'),source('73','cash')]),spec)
        self.assertEqual(table['filed_lines'][0]['status'],AMBIGUOUS)
        self.assertNotIn('statement_identity_rule_ref',table)

    def test_formula_tie_uses_its_exact_declared_output_statement(self):
        spec={'financial_statement_structure':{'lines':[{'ref':'operating','kind':'derived','concept':None,'statement':'income'}],
              'formulas':[{'output_ref':'operating','tie_out_concept':CONCEPT}]}}
        self.assertEqual(_declared_statement_hints(spec),{CONCEPT:'income'})
        spec['financial_statement_structure']['formulas'][0]['output_ref']='missing'
        self.assertEqual(_declared_statement_hints(spec),{})

    def test_unknown_statement_and_same_statement_conflicts_are_not_resolved(self):
        ledger=fixtures.FakeMissions([source('11'),source('73','cash')])
        self.assertEqual(_series_for(ledger,'company:ticker:other',CONCEPT,statement_hint='balance')['status'],AMBIGUOUS)
        conflicting=fixtures.FakeMissions([source('11'),source('12',line_id='line:second'),source('73','cash')])
        result=_series_for(conflicting,'company:ticker:other',CONCEPT,statement_hint='income')
        self.assertEqual(result['series']['quarters'],[])
        self.assertTrue(result['series']['ambiguous_periods'])

    def test_unambiguous_legacy_series_is_byte_equal(self):
        ledger=fixtures.FakeMissions([source('11')])
        self.assertEqual(canonical_json(_series_for(ledger,'company:ticker:other',CONCEPT)),
                         canonical_json(_series_for(ledger,'company:ticker:other',CONCEPT,statement_hint='income')))
        self.assertNotIn('statement_scope',_series_for(ledger,'company:ticker:other',CONCEPT))

    def test_full_financial_dag_replays_and_wrong_amount_still_refuses(self):
        # Reuse the existing full source-bound income fixture, preserving every
        # equation assertion while adding a conflicting presentation on cash.
        real=materialize_financial_statement_structure;captured={}
        def capture(spec,table,**kwargs):
            captured.update(spec=deepcopy(spec),table=deepcopy(table))
            return real(spec,table,**kwargs)
        fixture=fixtures.ModelInputTests()
        with patch.object(fixtures,'materialize_financial_statement_structure',side_effect=capture):
            fixture.test_built_inputs_materialize_the_persisted_statement_definition()
        spec=captured['spec'];table=captured['table']
        # The original table is unchanged: declaration alone does not add a marker.
        self.assertNotIn('statement_identity_rule_ref',table)
        concept='us-gaap:IncomeTaxExpenseBenefit'
        original=next(l for l in table['filed_lines']if l['concept']==concept)
        lines=[fixtures._line('us-gaap:Revenues','2026-03-01','2026-05-31','18718144000'),
               fixtures._line('us-gaap:CostOfGoodsAndServicesSold','2026-03-01','2026-05-31','12000000000'),
               fixtures._line('us-gaap:OperatingIncomeLoss','2026-03-01','2026-05-31','6718144000'),
               fixtures._line('us-gaap:IncomeBeforeTax','2026-03-01','2026-05-31','6718144000'),
               fixtures._line(concept,'2026-03-01','2026-05-31','1000000000'),
               fixtures._line(concept,'2026-03-01','2026-05-31','9000000000',statement='cash'),
               fixtures._line('us-gaap:NetIncomeLoss','2026-03-01','2026-05-31','5718144000')]
        derived=build_model_inputs(fixtures.FakeMissions(lines),spec);real(spec,derived)
        self.assertEqual(next(l for l in derived['filed_lines']if l['concept']==concept)['statement'],'income')
        lines[4]['value']='2000000000'
        invalid=build_model_inputs(fixtures.FakeMissions(lines),spec)
        with self.assertRaisesRegex(FinancialStatementStructureError,'does not tie'):real(spec,invalid)
