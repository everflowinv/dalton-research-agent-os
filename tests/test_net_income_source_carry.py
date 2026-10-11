"""An exact earnings fact retains its cash origin while tying income math."""
import copy
import unittest
from tests import test_company_financial_statement_structure as fixture
from dalton_core.company_financial_statement_structure import (
    FinancialStatementStructureError, financial_input_authority,
    validate_financial_statement_structure,
)
from dalton_core.store import canonical_json, content_hash


def carried_case(concept='us-gaap:NetIncomeLoss'):
    inputs=fixture.financial_inputs()
    row=next(x for x in inputs['filed_lines'] if x['concept']=='net')
    row.update(concept=concept, statement='cash')
    proposal=fixture.proposal(inputs)
    next(x for x in proposal['formulas'] if x['output_ref']=='net')['tie_out_concept']=concept
    return inputs,proposal


class NetIncomeSourceCarryTests(unittest.TestCase):
    def test_same_consolidated_fact_preserves_cash_cells_and_authority(self):
        for concept in ('us-gaap:NetIncomeLoss','us-gaap:ProfitLoss'):
            with self.subTest(concept=concept):
                inputs,candidate=carried_case(concept)
                before=canonical_json(inputs)
                authority=financial_input_authority(inputs)
                structure,replay=validate_financial_statement_structure(candidate,fixture.company_spec(),inputs)
                self.assertTrue(replay['ready_for_forecast'])
                self.assertEqual(canonical_json(inputs),before)
                self.assertEqual(structure['financial_input_hash'],authority['content_hash'])
                held=next(x for x in inputs['filed_lines'] if x['concept']==concept)
                self.assertEqual(held['statement'],'cash')
                self.assertTrue(all(c['source_accessions']==[fixture.ACCESSION] for c in held['cells'].values()))

    def test_separate_diluted_numerator_ties_same_exact_net_income_fact(self):
        inputs,candidate=carried_case()
        numerator=next(x for x in candidate['lines'] if x['ref']=='eps-numerator')
        numerator.update(kind='derived',concept=None,forecast_method='formula')
        candidate['formulas'].append(fixture.sum_formula(
            'eps-numerator',(('net',1),),'us-gaap:NetIncomeLoss'))
        net=next(x for x in inputs['filed_lines'] if x['concept']=='us-gaap:NetIncomeLoss')
        eps=next(x for x in inputs['filed_lines'] if x['concept']=='eps')
        shares=next(x for x in inputs['filed_lines'] if x['concept']=='shares')
        from decimal import Decimal
        for period,cell in eps['cells'].items():
            cell['value']=str(Decimal(net['cells'][period]['value'])/Decimal(shares['cells'][period]['value']))
        candidate['financial_input_hash']=financial_input_authority(inputs)['content_hash']
        _,replay=validate_financial_statement_structure(candidate,fixture.company_spec(),inputs)
        self.assertTrue(replay['ready_for_forecast'])

    def test_other_cash_fact_cannot_be_carried_by_label(self):
        inputs,candidate=carried_case('us-gaap:OtherNoncashIncomeExpense')
        with self.assertRaisesRegex(FinancialStatementStructureError,'tie-out statement differs'):
            validate_financial_statement_structure(candidate,fixture.company_spec(),inputs)

    def test_other_income_role_cannot_use_cash_net_income(self):
        inputs,candidate=carried_case()
        next(x for x in candidate['lines'] if x['ref']=='net')['role']='operating_income'
        with self.assertRaisesRegex(FinancialStatementStructureError,'tie-out statement differs'):
            validate_financial_statement_structure(candidate,fixture.company_spec(),inputs)

    def test_wrong_period_and_unit_remain_refused(self):
        for mutation,reason in (({'period_basis':'instant'},'statement differs'),
                                ({'cell_unit':'shares'},'unit differs')):
            with self.subTest(mutation=mutation):
                inputs,candidate=carried_case();line=next(x for x in inputs['filed_lines'] if x['concept']=='us-gaap:NetIncomeLoss')
                if 'cell_unit' in mutation:
                    for cell in line['cells'].values():cell['unit']=mutation['cell_unit']
                else:line.update(mutation)
                candidate['financial_input_hash']=financial_input_authority(inputs)['content_hash']
                with self.assertRaisesRegex(FinancialStatementStructureError,reason):
                    validate_financial_statement_structure(candidate,fixture.company_spec(),inputs)

    def test_cash_origin_does_not_relax_numeric_tie(self):
        inputs,candidate=carried_case();line=next(x for x in inputs['filed_lines'] if x['concept']=='us-gaap:NetIncomeLoss')
        for cell in line['cells'].values():cell['value']='999'
        candidate['financial_input_hash']=financial_input_authority(inputs)['content_hash']
        with self.assertRaisesRegex(FinancialStatementStructureError,'does not tie to filed history'):
            validate_financial_statement_structure(candidate,fixture.company_spec(),inputs)

    def test_native_income_structure_and_replay_bytes_stay_stable(self):
        inputs=fixture.financial_inputs();candidate=fixture.proposal(inputs)
        structure,replay=validate_financial_statement_structure(candidate,fixture.company_spec(),inputs)
        self.assertEqual(structure['content_hash'],'7761a3a00ed1bad7e891ef74d626d50e023af6bfea5a69f47a15fbe4ebc3074e')
        self.assertEqual(content_hash(replay),'8961f15afc5ded36c0c5dcc17c58e398fe07200c021ac96f70eee8713be27c03')
