"""Valuation reuses one strongly validated company snapshot, only within a scan."""
import copy
import unittest
from unittest.mock import patch

from dalton_core import valuation_snapshot_cli as valuation
from dalton_core.sec_filing_units import validate_unit_supplement, FilingUnitError
from tests import test_valuation_snapshot_lane as fixtures
from tests import test_statement_context_composition as composition


class LegacyStatementRows:
    """The original per-concept path, used to compare complete calculated inputs."""
    def __init__(self, missions, company_ref):
        self.missions, self.company_ref = missions, company_ref
    def filings(self):
        return self.missions.statement_filings(self.company_ref)
    def series(self, statement, concept):
        return self.missions.statement_series_lines(self.company_ref, concept, statement=statement)


class ScopedStatementReadTests(fixtures.ValuationLaneHarness):
    def filing_lines(self, periods, report_date, *, drop=()):
        rows = super().filing_lines(periods, report_date, drop=drop)
        for period in periods:
            if period in (fixtures.Q1, fixtures.Q2, fixtures.Q3):
                row = self.line("income", "us-gaap:WeightedAverageNumberOfDilutedSharesOutstanding",
                                "100", period, ordinal=len(rows))
                row["unit"] = "shares"
                rows.append(row)
        return rows

    def setUp(self):
        super().setUp()
        self.publish_prices()
        self.ingest_filings()
    def inputs(self):
        return valuation.build_inputs(missions=self.missions, prices=self.prices,
            company_ref=fixtures.ACN, calendar=[day for day, _ in fixtures._bars()])
    def test_complete_multifiling_multiconcept_inputs_equal_original_path(self):
        with patch.object(valuation, '_CompanyStatementRows', LegacyStatementRows):
            before = self.inputs()
        after = self.inputs()
        self.assertEqual(before, after)
        self.assertEqual(len(after['fundamental_windows']), 4)
        self.assertGreaterEqual(len(after['fundamental_windows'][-1]['roles']), 6)
        self.assertEqual(len(after['share_history']), 3)
        self.assertTrue(all(row['shares_outstanding'] == '100' for row in after['share_history']))
    def test_each_filing_is_read_once_and_next_calculation_revalidates(self):
        count = len(self.missions.statement_filings(fixtures.ACN))
        with patch.object(self.missions, 'statement_lines', wraps=self.missions.statement_lines) as lines, \
             patch.object(self.missions, 'statement_filings', wraps=self.missions.statement_filings) as filings:
            first = self.inputs()
            self.assertEqual(lines.call_count, count)
            self.assertEqual(filings.call_count, 1)
            second = self.inputs()
            self.assertEqual(lines.call_count, count * 2)
            self.assertEqual(filings.call_count, 2)
        self.assertEqual(first, second)
    def test_rows_keep_authority_identity_units_and_original_order(self):
        scoped = valuation._CompanyStatementRows(self.missions, fixtures.ACN)
        for statement, concept in [('income', 'us-gaap:Revenues'), ('cash', 'us-gaap:NetCashProvidedByUsedInOperatingActivities')]:
            self.assertEqual(scoped.series(statement, concept), self.missions.statement_series_lines(
                fixtures.ACN, concept, statement=statement))
        rows = scoped.series('income', 'us-gaap:Revenues')
        self.assertTrue(all(row['unit'] == 'usd' and row['line_id'] and row['ingest_id'] for row in rows))
    def test_authority_refusal_still_aborts_fundamental_calculation(self):
        with patch.object(self.missions, 'statement_lines', side_effect=ValueError('original source drift')):
            with self.assertRaisesRegex(ValueError, 'original source drift'):
                self.inputs()
    def test_optional_share_history_preserves_refusal_semantics(self):
        with patch.object(self.missions, 'statement_lines', side_effect=ValueError('original source drift')):
            self.assertEqual(valuation.share_history_from_filings(self.missions, fixtures.ACN), [])


class SourceReplayTests(unittest.TestCase):
    def test_new_scoped_read_rejects_original_xml_drift(self):
        f = composition.CompositionTests('test_both_strong_proofs_pass_and_old_hash_remains_refused')
        f.setUp()
        self.addCleanup(f.doCleanups)
        record = f.record()
        class Missions:
            def statement_filings(self, _company):
                return [{**f.filing, 'ingest_id': 'synthetic-ingest'}]
            def statement_lines(self, _ingest):
                validate_unit_supplement(f.c, record, f.filing)
                return copy.deepcopy(record['lines'])
        with patch('dalton_core.sec_filing_units._source', return_value=(f.source, f.xml)):
            original = valuation._CompanyStatementRows(Missions(), 'company:synthetic')
            self.assertTrue(original.series('income', 'us-gaap:Revenues'))
        altered = f.xml.replace(b'1,234.5', b'1,234.6')
        self.assertNotEqual(altered, f.xml)
        with patch('dalton_core.sec_filing_units._source', return_value=(f.source, altered)):
            with self.assertRaisesRegex(FilingUnitError, 'source hash drifted'):
                valuation._CompanyStatementRows(Missions(), 'company:synthetic').series('income', 'us-gaap:Revenues')


if __name__ == '__main__':
    unittest.main()
