import unittest
from dalton_core.document_numeric_claim import verify_numeric_candidate, NumericCandidateError
from dalton_core.document_numeric_unit_contracts import numeric_units, figure_numeric_contract
from dalton_core.operating_driver_units import CONTRACT_REF, FIGURE_VERIFIER_REF


class OperatingUnitsTests(unittest.TestCase):
    def fact(self, **kw):
        return dict(quote_id='q1', metric_ref='metric:units-sold', subject_as_named='Example',
                    as_reported_label='Units sold', value='40', unit='tons', currency=None,
                    period='2026-01-01/2026-03-31', basis='management-reported', scale=None, **kw)

    def test_old_unit_contracts_unchanged_and_unknown_ref_refused(self):
        self.assertEqual(numeric_units(), ('percent','currency','count','ratio','days','basis_points'))
        self.assertEqual(numeric_units('directed-document-numeric:0.1'), (*numeric_units(),'years'))
        self.assertNotIn('tons', numeric_units('directed-document-numeric:0.1'))
        self.assertEqual(figure_numeric_contract(FIGURE_VERIFIER_REF), CONTRACT_REF)
        with self.assertRaises(ValueError): numeric_units('directed-document-operating-numeric:0.2')

    def test_exact_physical_unit_kept_not_converted_to_count(self):
        quote='Example 2026-01-01/2026-03-31 Units sold were 40 tons.'
        got=verify_numeric_candidate(self.fact(), {'q1':quote}, numeric_contract_ref=CONTRACT_REF)
        self.assertEqual(got['unit'],'tons'); self.assertEqual(got['value'],'40')
        with self.assertRaises(NumericCandidateError):
            verify_numeric_candidate(self.fact(), {'q1':quote}, numeric_contract_ref='directed-document-numeric:0.1')

    def test_qualified_unit_and_other_sentence_cannot_supply_unit(self):
        for quote in ('Units sold were 40 metric tons in 2026-01-01/2026-03-31.',
                      'Units sold were 40 in 2026-01-01/2026-03-31. Other goods weighed 12 tons.',
                      'Units sold were 40 in 2026-01-01/2026-03-31. Inventory tons | 12.'):
            with self.subTest(quote=quote),self.assertRaises(NumericCandidateError):
                verify_numeric_candidate(self.fact(),{'q1':quote},numeric_contract_ref=CONTRACT_REF)

    def test_named_table_row_multiplier_remains_explicit(self):
        quote='2026-01-01/2026-03-31\nUnits sold (thousands of tons) | 40'
        fact=self.fact(); fact['scale']='thousand'
        self.assertEqual(verify_numeric_candidate(fact, {'q1':quote},numeric_contract_ref=CONTRACT_REF)['scale'],'thousand')
        for scale in (None,'million'):
            fact['scale']=scale
            with self.assertRaises(NumericCandidateError):
                verify_numeric_candidate(fact, {'q1':quote},numeric_contract_ref=CONTRACT_REF)

    def test_price_is_quantity_denominated_not_revenue(self):
        fact=self.fact();fact.update(metric_ref='metric:selling-price',as_reported_label='Selling price',
                                    unit='currency_per_ton',currency='USD')
        quote='2026-01-01/2026-03-31 Selling price was USD 40 per ton.'
        got=verify_numeric_candidate(fact,{'q1':quote},numeric_contract_ref=CONTRACT_REF)
        self.assertEqual(got['unit'],'currency_per_ton');self.assertEqual(got['currency'],'USD')
        for wrong in ('2026-01-01/2026-03-31 Selling price was EUR 40 per ton. USD revenue was 80.',
                      '2026-01-01/2026-03-31 Selling price was $40 per ton.',
                      '2026-01-01/2026-03-31 Selling price was USD 40 per metric ton.'):
            with self.subTest(quote=wrong),self.assertRaises(NumericCandidateError):
                verify_numeric_candidate(fact,{'q1':wrong},numeric_contract_ref=CONTRACT_REF)


    def test_duration_wording_is_exact_not_report_date_or_same_year(self):
        quote='Example three months ended March 31, 2026 Units sold (tons) | 40'
        self.assertEqual(verify_numeric_candidate(self.fact(),{'q1':quote},numeric_contract_ref=CONTRACT_REF)['value'],'40')
        for changed in ('Example fiscal year 2026 Units sold (tons) | 40',
                        'Example six months ended March 31, 2026 Units sold (tons) | 40',
                        quote+'; three months ended March 31, 2025 Units sold (tons) | 38',
                        'Example three months ended March 30, 2026 Units sold (tons) | 40'):
            with self.subTest(quote=changed),self.assertRaises(NumericCandidateError):
                verify_numeric_candidate(self.fact(),{'q1':changed},numeric_contract_ref=CONTRACT_REF)

if __name__ == '__main__': unittest.main()
