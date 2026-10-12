"""Exact source supplements preserve concepts, complete contexts and old paid roots."""
import copy
import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dalton_core.sec_filing_units import FilingUnitError, supplemental_unit_lines, validate_unit_supplement
from dalton_core.statement_original_revenue_context import (
    RULE_REF, supplemental_revenue_lines, validate_revenue_supplement, prepare_model_revenue_inputs,
)
from dalton_core.store import content_hash

XML = '''<html xmlns="http://www.w3.org/1999/xhtml" xmlns:x="http://www.xbrl.org/2003/instance" xmlns:d="http://xbrl.org/2006/xbrldi" xmlns:currency="http://www.xbrl.org/2003/iso4217" xmlns:ix="http://www.xbrl.org/2013/inlineXBRL" xmlns:us-gaap="http://fasb.org/us-gaap/2026" xmlns:issuer="http://example.com/taxonomy/2026"><body><x:unit id="u"><x:measure>currency:USD</x:measure></x:unit><x:context id="total"><x:entity><x:identifier scheme="http://www.sec.gov/CIK">12</x:identifier></x:entity><x:period><x:startDate>2026-01-01</x:startDate><x:endDate>2026-03-31</x:endDate></x:period></x:context><x:context id="member"><x:entity><x:identifier scheme="http://www.sec.gov/CIK">12</x:identifier><x:segment><d:explicitMember dimension="us-gaap:StatementBusinessSegmentsAxis">issuer:NorthMember</d:explicitMember><d:explicitMember dimension="issuer:FixedAxis">issuer:FixedMember</d:explicitMember></x:segment></x:entity><x:period><x:startDate>2026-01-01</x:startDate><x:endDate>2026-03-31</x:endDate></x:period></x:context><ix:nonFraction name="us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax" contextRef="total" unitRef="u">100</ix:nonFraction><ix:nonFraction name="us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax" contextRef="member" unitRef="u">60</ix:nonFraction></body></html>'''


def old_row():
    return dict(statement='income', concept='us-gaap:Revenues', label='Revenue', level=0,
                parent_concept=None, is_breakdown=False, dimension_axis=None, dimension_member=None,
                dimension_count=0, dimensions={}, period_start='2026-01-01', period_end='2026-03-31',
                value='100', unit='USD', balance='credit')


class OriginalRevenueTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name)
        self.c = sqlite3.connect(self.state/'core.sqlite'); self.c.row_factory = sqlite3.Row
        self.addCleanup(self.c.close)
        self.filing = dict(ingest_id='filing', company_ref='company:other', cik='12',
                           accession='0000000012-26-000001', form='10-Q', filed='2026-04-01', report_date='2026-03-31')
        self.source = {'record_id': 'exact-primary', 'manifest_hash': 'a'*64}

    def supplement(self, xml=XML, lines=None):
        with patch('dalton_core.sec_filing_units._source', return_value=(self.source, xml.encode())):
            return supplemental_revenue_lines(self.c, self.state, self.filing, [old_row()] if lines is None else lines)

    def record(self, xml=XML):
        rows, proof = self.supplement(xml)
        with patch('dalton_core.sec_filing_units._source', return_value=(self.source, xml.encode())):
            resolved, units = supplemental_unit_lines(self.c, self.state, self.filing, rows)
        return {'kind': 'complete_snapshot', 'lines': resolved,
                'supplementary_original_revenue': proof, 'supplementary_units': units}

    def replay(self, record, xml=XML):
        with patch('dalton_core.sec_filing_units._source', return_value=(self.source, xml.encode())):
            validate_unit_supplement(self.c, record, self.filing)
            validate_revenue_supplement(self.c, record, self.filing)

    def test_other_issuer_exact_own_total_all_dimensions_no_alias_or_residual(self):
        rows, proof = self.supplement()
        self.assertEqual(rows[0], old_row())
        self.assertEqual(sorted(r['value'] for r in rows), ['100', '100', '60'])
        self.assertEqual({r['concept'] for r in rows[1:]}, {'us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax'})
        member = next(r for r in rows if r['dimension_count'] == 2)
        self.assertEqual(member['dimensions'], {'issuer:FixedAxis':'issuer:FixedMember',
                                               'us-gaap:StatementBusinessSegmentsAxis':'issuer:NorthMember'})
        self.assertIsNone(member['dimension_axis'])
        self.assertEqual({e['unit']['unit'] for e in proof['evidence']}, {'USD'})
        self.replay(self.record())

    def test_duplicate_presentations_do_not_duplicate_members_and_conflicts_reject(self):
        duplicate = XML.replace('</body>', '<ix:nonFraction name="us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax" contextRef="member" unitRef="u">60</ix:nonFraction></body>')
        self.assertEqual(len(self.supplement(duplicate)[0]), 3)
        with self.assertRaisesRegex(FilingUnitError, 'conflicting'):
            self.supplement(duplicate.replace('>60</ix:nonFraction></body>', '>61</ix:nonFraction></body>'))

    def test_existing_equal_identity_unchanged_conflict_is_not_overwritten(self):
        rows, _ = self.supplement()
        again, proof = self.supplement(lines=rows)
        self.assertEqual(again, rows); self.assertIsNone(proof)
        bad = copy.deepcopy(rows); bad[1]['value'] = '101'
        with self.assertRaisesRegex(FilingUnitError, 'existing exact'):
            self.supplement(lines=bad)

    def test_unsupported_typed_member_unknown_unit_missing_context_are_not_hidden(self):
        for xml in [XML.replace('<x:segment>', '<x:segment><d:typedMember dimension="issuer:T"><issuer:value>a</issuer:value></d:typedMember>'),
                    XML.replace('currency:USD', 'x:pure'),
                    XML.replace('contextRef="member"', 'contextRef="unknown"'),
                    XML.replace('unitRef="u"', 'unitRef="unknown"')]:
            with self.subTest(xml=xml[-150:]), self.assertRaises(FilingUnitError):
                self.supplement(xml)

    def test_foreign_issuer_not_admitted_and_future_or_invalid_period_refuses(self):
        self.assertEqual(self.supplement(XML.replace('>12<', '>13<'))[0], [old_row()])
        for xml in [XML.replace('2026-03-31','2026-04-01'), XML.replace('2026-01-01','invalid'),
                    XML.replace('2026-01-01','2026-04-01')]:
            with self.assertRaises(FilingUnitError): self.supplement(xml)

    def test_source_bytes_fact_fields_and_self_consistent_forgery_rejected(self):
        record = self.record(); self.replay(record)
        with self.assertRaises(FilingUnitError): self.replay(record, XML.replace('>60<','>61<'))
        for key, val in [('value', '61'), ('period_end','2026-03-30'), ('concept','us-gaap:Revenues'),
                         ('dimensions', {}), ('unit','EUR'), ('label','invented')]:
            bad = copy.deepcopy(record); bad['lines'][-1][key] = val
            # A newly hashed authority envelope still cannot change a raw fact.
            bad['supplementary_original_revenue']['evidence'][-1]['line_hash'] = content_hash(bad['lines'][-1])
            bad['supplementary_original_revenue']['evidence_hash'] = content_hash(bad['supplementary_original_revenue']['evidence'])
            with self.subTest(key=key), self.assertRaises(FilingUnitError): self.replay(bad)

    def test_unit_proof_not_optional_for_new_currency_source_rows(self):
        bad = self.record(); bad.pop('supplementary_units')
        with self.assertRaises(FilingUnitError): self.replay(bad)

    def test_primary_missing_preserves_legacy_no_new_source_fields(self):
        with patch('dalton_core.sec_filing_units._source', side_effect=FilingUnitError('missing original')):
            rows, proof = supplemental_revenue_lines(self.c, self.state, self.filing, [old_row()])
        self.assertEqual(rows, [old_row()]); self.assertIsNone(proof)
        validate_revenue_supplement(None, {'kind': 'ordinal_overlay'}, {})
        self.assertEqual(self.supplement('<html><body>legacy plain filing</body></html>'), ([old_row()], None))

    def test_script_and_nil_do_not_become_amounts(self):
        script = XML.replace('<ix:nonFraction name=', '<script><ix:nonFraction name=', 1).replace('>100</ix:nonFraction>', '>100</ix:nonFraction></script>', 1)
        rows, _ = self.supplement(script)
        self.assertEqual(len(rows), 2)
        nil = XML.replace('xmlns:ix=', 'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xmlns:ix=').replace('contextRef="member"', 'contextRef="member" xsi:nil="true"').replace('>60</ix:nonFraction>', '></ix:nonFraction>')
        self.assertEqual(len(self.supplement(nil)[0]), 2)
        self.assertEqual(len(self.supplement(nil.replace('></ix:nonFraction>', '>60</ix:nonFraction>'))[0]), 2)
        unrelated = XML.replace('</body>', '<ix:nonFraction name="unbound:OtherMetric" contextRef="missing" unitRef="missing">1</ix:nonFraction></body>')
        self.assertEqual(len(self.supplement(unrelated)[0]), 3)

    def test_new_rule_does_not_change_task_or_repair_contract_paid_replay_identity(self):
        from dalton_core.company_model_cli import REPAIR_CONTRACT_REF, REPAIR_CONTRACT_HASH, model_spec_validation_contract
        from dalton_core.company_model_spec import TASK_HASH
        self.assertEqual(REPAIR_CONTRACT_REF, 'contract:company-model-spec-structured-output-repair:0.13')
        self.assertEqual(TASK_HASH, '608338bcd6cf70cbe6e012acd5f579b99d34f9d990d5bc846c4059a198eda9af')
        self.assertEqual(model_spec_validation_contract()['original_revenue_context'], RULE_REF)
        self.assertEqual(REPAIR_CONTRACT_HASH, '3c5fa10ea1a061644a44bf9f49edc6a290ee39e34b616a86269ad4a481ec1679')
        from dalton_core import statement_context as context
        # The additive rule is not a blanket revision of every old context.
        # Changing this helper's bytes cannot change the base normalizer hash.
        expected = context.normalizer_hash()
        with patch('dalton_core.statement_original_revenue_context.RULE_REF', 'future-rule'):
            self.assertEqual(context.normalizer_hash(), expected)

    def test_bounded_preflight_no_additions_does_not_publish_new_context(self):
        class Missions:
            connection = self.c
            store = object()
            def statement_filings(inner, company): return [dict(self.filing, ingest_id=str(i)) for i in range(12)]
        with patch('dalton_core.statement_context._latest', return_value=None), \
             patch('dalton_core.statement_context.prepare_context', return_value={'status':'available'}) as prepare, \
             patch('dalton_core.statement_context.StatementContextAuthority') as authority:
            result = prepare_model_revenue_inputs(Missions(), self.state, 'company:other')
        self.assertFalse(result['refreshed']); self.assertEqual(prepare.call_count, 8)
        authority.assert_not_called()


class AppendReplayTests(unittest.TestCase):
    def test_normal_append_strong_read_old_rows_preserved_and_repeat_reuses(self):
        from tests import test_ar_segment_bridge_identity as fixture
        from dalton_core.store import DaltonStore
        from dalton_core.sec_financials_normalise import normalise_filing
        from dalton_core.statement_context import StatementContextAuthority, context_record, statement_rows
        f = fixture.ContextBackfillTests('test_context_overlay_is_exact_append_only_and_preserves_old_proof_resolution')
        f.setUp(); self.addCleanup(f.doCleanups)
        payload = {'cik':'0000999999','entity_name':'Synthetic Company','filings':[
            {'accession':'0000999999-26-000001','form':'10-Q','filed':'2026-08-01','report_date':'2026-06-30',
             'statements':{'income':{'structure':[{'concept':'us-gaap_Revenues','label':'Revenue','level':1,
                'parent_concept':None,'abstract':False,'is_breakdown':False,'balance':'credit'}],
                'facts':[{'concept':'us-gaap:Revenues','period_start':'2026-04-01','period_end':'2026-06-30',
                         'value':'100','unit_ref':'u','balance':'credit','dim_srt_ConsolidationItemsAxis':None}]}}}]}
        f.raw_parse(payload)
        original = normalise_filing(accession=payload['filings'][0]['accession'],form='10-Q',filed='2026-08-01',
                                    report_date='2026-06-30',statements=payload['filings'][0]['statements'])['lines']
        filing,_ = f.ingest(original)
        copydb = sqlite3.connect(f.state_dir/'core.sqlite'); f.store.connection.backup(copydb); copydb.close()
        store = DaltonStore(str(f.state_dir/'core.sqlite')); self.addCleanup(store.close)
        before = [dict(r) for r in store.connection.execute('SELECT * FROM coverage_mission_statement_lines')]
        xml = XML.replace('>12<','>999999<').replace('2026-01-01','2026-04-01').replace('2026-03-31','2026-06-30')
        xml = xml.replace('</body>', '<ix:nonFraction name="us-gaap:Revenues" contextRef="total" unitRef="u">100</ix:nonFraction></body>')
        with patch('dalton_core.sec_filing_units._source',return_value=({'record_id':'exact-primary'},xml.encode())):
            from dalton_core.company_model_cli import choose_company, run_model_spec
            from dalton_core.coverage_mission import CoverageMissionAuthority
            missions = CoverageMissionAuthority(store)
            pointer = store.connection.execute('SELECT mission_version_id FROM coverage_mission_pointer').fetchone()
            mission = missions.mission(pointer[0])
            _, old_state = choose_company(missions,mission,company_ref=filing['company_ref'])
            with patch('dalton_core.company_model_cli.CockpitModel',side_effect=AssertionError('stale source sent model')):
                summary = run_model_spec(state_dir=f.state_dir,model_config_path=None,
                                         summary_dir=f.state_dir/'source-preflight-summary',scheduler_db=None,
                                         company_ref=filing['company_ref'],expected_state_hash=old_state['state_hash'])
            self.assertEqual(summary['spec_status'],'stale_input',summary)
            self.assertNotEqual(summary['state_hash'],old_state['state_hash'])
            self.assertEqual(summary['cost_micros'],0)
            self.assertEqual(summary['formal_authority_writes'],0)
            self.assertTrue(summary['original_revenue_preflight']['refreshed'])
            authority = StatementContextAuthority(store)
            record = authority.backfill(f.state_dir,filing['ingest_id'],discover_companyfacts=False)
            self.assertEqual(record['status'],'available',record.get('reason'))
            self.assertEqual(record['supplementary_original_revenue']['base_line_count'],1)
            self.assertEqual(context_record(store.connection,record['version_id']),record)
            self.assertEqual(len(statement_rows(store.connection,filing['ingest_id'])),3)
            self.assertEqual(authority.backfill(f.state_dir,filing['ingest_id'],discover_companyfacts=False)['version_id'],record['version_id'])
            class Missions:
                connection = store.connection
                def statement_filings(inner, company):return [filing]
            with patch('dalton_core.statement_context.prepare_context',side_effect=AssertionError('unchanged proof reparsed original')):
                self.assertEqual(prepare_model_revenue_inputs(Missions(),f.state_dir,filing['company_ref']),
                                 {'refreshed':False,'context_refs':[]})
        after = [dict(r) for r in store.connection.execute('SELECT * FROM coverage_mission_statement_lines')]
        self.assertEqual(before,after)
        self.assertEqual(store.connection.execute('SELECT COUNT(*) FROM statement_context_versions').fetchone()[0],1)
        with patch('dalton_core.sec_filing_units._source',return_value=({'record_id':'exact-primary'},xml.replace('>60<','>61<').encode())):
            with self.assertRaises(FilingUnitError):context_record(store.connection,record['version_id'])
