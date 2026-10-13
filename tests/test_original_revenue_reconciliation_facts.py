"""Separate reported revenue components are facts, not aliases or residuals."""
import copy
import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dalton_core.sec_filing_units import supplemental_unit_lines, validate_unit_supplement, FilingUnitError
from dalton_core.statement_original_revenue_context import (
    RULE_REF, LEGACY_RULE_REF, RECONCILIATION_CONCEPTS,
    supplemental_revenue_lines, validate_revenue_supplement, prepare_model_revenue_inputs,
)
from dalton_core.model_forecast_driver import CONCEPT_ROLES, REVENUE
from dalton_core.store import canonical_json, content_hash
from tests import test_statement_original_revenue_context as base


def document(value):
    # Other income/OCI entries are deliberately not interchangeable with the
    # separately filed non-contract-revenue component, even at equal values.
    return base.XML.replace('</body>',
        '<ix:nonFraction name="us-gaap:RevenueNotFromContractWithCustomer" contextRef="total" unitRef="u">'+str(value)+'</ix:nonFraction>'
        '<ix:nonFraction name="us-gaap:OtherComprehensiveIncomeLossCashFlowHedgeGainLossReclassificationAfterTax" contextRef="total" unitRef="u">'+str(value)+'</ix:nonFraction></body>')


class ReconciliationFactTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name); self.c = sqlite3.connect(self.state/'core.sqlite')
        self.c.row_factory = sqlite3.Row; self.addCleanup(self.c.close)
        self.source = {'record_id':'other-issuer-original'}
        self.filing = dict(cik='12',report_date='2026-03-31',ingest_id='filing',company_ref='company:unrelated')

    def create(self, value, rule=RULE_REF):
        xml = document(value).encode()
        with patch('dalton_core.sec_filing_units._source',return_value=(self.source,xml)):
            rows, proof = supplemental_revenue_lines(self.c,self.state,self.filing,[base.old_row()],rule_ref=rule)
            rows, units = supplemental_unit_lines(self.c,self.state,self.filing,rows)
        return {'kind':'complete_snapshot','lines':rows,'supplementary_original_revenue':proof,'supplementary_units':units}

    def replay(self, r, value):
        with patch('dalton_core.sec_filing_units._source',return_value=(self.source,document(value).encode())):
            validate_unit_supplement(self.c,r,self.filing)
            validate_revenue_supplement(self.c,r,self.filing)

    def test_reported_positive_negative_and_zero_are_separate_source_concepts(self):
        for value in ('10','-7','0'):
            with self.subTest(value=value):
                record=self.create(value);self.replay(record,value)
                rows=[r for r in record['lines']if r['concept']in RECONCILIATION_CONCEPTS]
                self.assertEqual(len(rows),1);self.assertEqual(rows[0]['value'],value)
                self.assertEqual(rows[0]['unit'],'USD');self.assertEqual(rows[0]['dimensions'],{})
                self.assertEqual(rows[0]['period_start'],'2026-01-01')
                self.assertNotEqual(CONCEPT_ROLES.get(rows[0]['concept']),REVENUE)
                self.assertNotIn('OtherComprehensiveIncome',canonical_json(record))
                self.assertEqual(record['lines'][0],base.old_row())

    def test_v1_frozen_replay_does_not_silently_gain_new_facts(self):
        record=self.create('10',LEGACY_RULE_REF);before=copy.deepcopy(record)
        self.replay(record,'10');self.assertEqual(record,before)
        self.assertFalse(any(r['concept']in RECONCILIATION_CONCEPTS for r in record['lines']))
        hostile=copy.deepcopy(record);hostile['supplementary_original_revenue']['rule_ref']=RULE_REF
        with self.assertRaises(FilingUnitError):self.replay(hostile,'10')

    def test_oci_substitution_changed_value_or_source_unit_rejected(self):
        original=self.create('10')
        with self.assertRaises(FilingUnitError):self.replay(original,'11')
        for key,value in [('concept','us-gaap:OtherComprehensiveIncomeLossCashFlowHedgeGainLossReclassificationAfterTax'),('unit','EUR'),('period_end','2026-04-01')]:
            bad=copy.deepcopy(original);row=next(r for r in bad['lines']if r['concept']in RECONCILIATION_CONCEPTS);row[key]=value
            with self.subTest(key=key),self.assertRaises(FilingUnitError):self.replay(bad,'10')

    def test_new_rule_without_new_material_does_not_republish_v1_context(self):
        record=self.create('10',LEGACY_RULE_REF)
        # A source with no supplemental reconciliation fact has identical
        # material under the two versions, even though their proof hashes differ.
        with patch('dalton_core.sec_filing_units._source',return_value=(self.source,base.XML.encode())):
            lines,proof=supplemental_revenue_lines(self.c,self.state,self.filing,[base.old_row()],rule_ref=LEGACY_RULE_REF)
            current_lines,current_proof=supplemental_revenue_lines(self.c,self.state,self.filing,[base.old_row()])
        self.assertEqual(lines,current_lines);self.assertNotEqual(proof,current_proof)
        old={'kind':'complete_snapshot','status':'available','ingest_id':'filing','version_id':'old-context',
             'lines':lines,'mapping_hash':content_hash(lines),'supplementary_original_revenue':proof}
        old['content_hash']=content_hash(old)
        self.c.execute('CREATE TABLE statement_context_versions(version_id,record_json,content_hash)')
        self.c.execute('INSERT INTO statement_context_versions VALUES(?,?,?)',('old-context',canonical_json(old),old['content_hash']))
        class Missions:
            connection=self.c
            def statement_filings(inner,company):return [dict(self.filing,filed='2026-04-01')]
        with patch('dalton_core.statement_context._latest',return_value='old-context'), \
             patch('dalton_core.statement_context.prepare_context',return_value={'status':'available','mapping_hash':content_hash(current_lines),'supplementary_original_revenue':current_proof}), \
             patch('dalton_core.statement_context.StatementContextAuthority')as authority:
            self.assertEqual(prepare_model_revenue_inputs(Missions(),self.state,'company:unrelated'),{'refreshed':False,'context_refs':[]})
            authority.assert_not_called()

    def test_bounded_numeric_prompt_keeps_component_with_own_unit_period_and_binding(self):
        from dalton_core.company_model_state import _numeric_context,DEFAULT_NUMERIC_CONTEXT_POLICY
        from dalton_core.company_model_spec import _numeric_period_table
        record=self.create('-7')
        lines=[{**row,'ingest_id':'filing','ordinal':i,'line_id':'filing#source-context#'+str(i),
                'statement_context_ref':'context:exact'}for i,row in enumerate(record['lines'])]
        filing={'ingest_id':'filing','accession':'0000000012-26-000001','form':'10-Q','filed':'2026-04-01',
                'report_date':'2026-03-31','content_hash':'a'*64,'source_record_refs':['source:exact']}
        ctx,_=_numeric_context([filing],{'filing':lines},structure_series={('income',row['concept'])for row in lines},policy=DEFAULT_NUMERIC_CONTEXT_POLICY)
        row=next(c for c in ctx['cells']if c['concept']in RECONCILIATION_CONCEPTS)
        self.assertEqual((row['value'],row['unit'],row['period_start'],row['period_end']),('-7','USD','2026-01-01','2026-03-31'))
        self.assertEqual(row['statement_context_ref'],'context:exact')
        self.assertEqual(row['filing_content_hash'],'a'*64)
        self.assertTrue(row['unit_definition_hash']);self.assertEqual(row['source_unit_ref'],'u')
        prompt=_numeric_period_table({'numeric_context':ctx})
        self.assertIn('us-gaap:RevenueNotFromContractWithCustomer',prompt)
        self.assertNotIn('OtherComprehensiveIncome',prompt)
        self.assertLess(len(prompt.encode()),120000)
