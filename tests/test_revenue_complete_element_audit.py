"""Complete source elements, not streaming tail availability, bind facts."""
import copy
import io
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from xml.etree import ElementTree as ET

from dalton_core import sec_filing_units as units
from dalton_core import statement_original_revenue_context as revenue
from dalton_core.store import content_hash
from tests.test_statement_original_revenue_context import XML, old_row


class CompleteElementAuditTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name)
        self.c = sqlite3.connect(self.state/'core.sqlite'); self.c.row_factory = sqlite3.Row
        self.addCleanup(self.c.close)
        self.filing = {'cik':'12', 'report_date':'2026-03-31'}
        self.source = {'record_id':'exact-primary', 'manifest_hash':'a'*64}

    def derive(self, raw, *, rule=revenue.RULE_REF, chunk=1):
        parsed = units.parse_filing_units(raw, rule_ref=units.NUMERIC_RULE_REF)
        original_iterparse = ET.iterparse
        class Chunked(io.BytesIO):
            def read(inner, n=-1):
                return super().read(min(n, chunk) if n >= 0 else chunk)
        def iterparse(source, events):
            return original_iterparse(Chunked(source.getvalue()) if isinstance(source, io.BytesIO) else source, events=events)
        with patch.object(revenue.ET, 'iterparse', side_effect=iterparse):
            return revenue.derive_lines(self.filing, [old_row()], parsed, raw, rule_ref=rule), parsed

    def record(self, raw, rule):
        with patch('dalton_core.sec_filing_units._source', return_value=(self.source, raw)):
            lines, proof = revenue.supplemental_revenue_lines(self.c, self.state, self.filing, [old_row()], rule_ref=rule)
            lines, unit_proof = units.supplemental_unit_lines(self.c, self.state, self.filing, lines)
        return {'kind':'complete_snapshot', 'lines':lines, 'supplementary_units':unit_proof,
                'supplementary_original_revenue':proof}

    def replay(self, raw, record):
        with patch('dalton_core.sec_filing_units._source', return_value=(self.source, raw)):
            units.validate_unit_supplement(self.c, record, self.filing)
            revenue.validate_revenue_supplement(self.c, record, self.filing)

    def test_chunk_boundaries_and_tail_text_preserve_every_parser_fact_hash(self):
        for issuer in ('12', '987'):
            self.filing['cik'] = issuer
            for tail in ('\u00a0', '\n outside fact 999 ', ''):
                raw = XML.replace('>12<', '>'+issuer+'<').replace('</ix:nonFraction>', '</ix:nonFraction>'+tail).encode()
                outputs = []
                for chunk in (1, 7, 16384):
                    (rows, evidence), parsed = self.derive(raw, chunk=chunk)
                    self.assertEqual(sorted(r['value'] for r in rows), ['100', '100', '60'])
                    self.assertTrue({e['fact']['fact_sha256'] for e in evidence} <= {f['fact_sha256'] for f in parsed['facts']})
                    outputs.append((rows, evidence))
                self.assertEqual(outputs[0], outputs[1]); self.assertEqual(outputs[0], outputs[2])
        raw = XML.replace('</ix:nonFraction>', '</ix:nonFraction>\u00a0').encode()
        self.filing['cik'] = '12'
        with self.assertRaisesRegex(units.FilingUnitError, 'numeric/context/unit'):
            self.derive(raw, rule=revenue.FIXED_ZERO_RULE_REF)

    def test_existing_fixed_zero_namespace_rules_do_not_become_a_text_fallback(self):
        raw = XML.replace('xmlns:ix=', 'xmlns:t="'+units.FIXED_ZERO_NAMESPACE+'" xmlns:ix=').replace(
            'contextRef="member"', 'contextRef="member" format="t:fixed-zero"').replace('>60</ix:nonFraction>', '>—</ix:nonFraction>\u00a0').encode()
        (rows, evidence), parsed = self.derive(raw)
        self.assertEqual({r['value'] for r in rows}, {'100','0'})
        self.assertEqual(parsed['rule_ref'], units.NUMERIC_RULE_REF)
        for bad in (raw.replace(units.FIXED_ZERO_NAMESPACE.encode(), b'http://wrong.example'),
                    raw.replace(b'format="t:', b'format="unbound:'),
                    raw.replace(b'unitRef="u"', b'unitRef="unknown"'),
                    raw.replace(b'contextRef="member"', b'contextRef="unknown"')):
            with self.subTest(bad=bad[-90:]), self.assertRaises(units.FilingUnitError):
                self.derive(bad)

    def test_nil_script_and_unbound_revenue_keep_strict_original_boundaries(self):
        raw = XML.replace('xmlns:ix=', 'xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xmlns:ix=').replace(
            'contextRef="member"', 'contextRef="member" xsi:nil="true"').replace('>60</ix:nonFraction>', '>—</ix:nonFraction>\u00a0').encode()
        self.assertEqual(len(self.derive(raw)[0][0]), 2)
        script = XML.replace('<ix:nonFraction name=', '<script><ix:nonFraction name=', 1).replace(
            '>100</ix:nonFraction>', '>100</ix:nonFraction></script>\u00a0', 1).encode()
        self.assertEqual(len(self.derive(script)[0][0]), 2)
        with self.assertRaisesRegex(units.FilingUnitError, 'unbound concept'):
            self.derive(XML.replace('name="us-gaap:', 'name="unbound:').encode())

    def test_rules_one_two_three_keep_exact_historical_proof_bytes_and_strong_replay(self):
        raw = XML.encode()
        expected = {
            revenue.LEGACY_RULE_REF:'aeea2c35810b45d30b8fb8008a4b859071e715d3fef04817242b475bb3ce1735',
            revenue.RECONCILIATION_RULE_REF:'2d641fcfb8779af3cebfffcf57aff959f76c71f4317528f0b941544cb26b7388',
            revenue.FIXED_ZERO_RULE_REF:'755daf653c07a28996c1cddecf768ab6812d58511d23f264c52d366282e47fce',
        }
        for rule, digest in expected.items():
            with patch('dalton_core.sec_filing_units._source', return_value=(self.source, raw)):
                result = revenue.supplemental_revenue_lines(self.c, self.state, self.filing, [old_row()], rule_ref=rule)
            self.assertEqual(content_hash(result), digest)
            record = self.record(raw, rule); before = copy.deepcopy(record)
            self.replay(raw, record); self.assertEqual(record, before)
        with patch('dalton_core.sec_filing_units._source', return_value=(self.source, raw)), self.assertRaises(units.FilingUnitError):
            revenue.supplemental_revenue_lines(self.c, self.state, self.filing, [old_row()], rule_ref='future')

    def test_new_proof_full_source_unit_period_and_amount_replay_still_required(self):
        raw = XML.replace('</ix:nonFraction>', '</ix:nonFraction>\u00a0').encode()
        record = self.record(raw, revenue.RULE_REF); self.replay(raw, record)
        self.assertEqual(record['supplementary_original_revenue']['rule_ref'], revenue.RULE_REF)
        self.assertEqual(record['supplementary_original_revenue']['parsed_hash'], content_hash(units.parse_filing_units(raw, rule_ref=units.NUMERIC_RULE_REF)))
        for bad_raw in (raw.replace(b'outside',b'changed'), raw.replace(b'>60<',b'>61<'),
                        raw.replace(b'currency:USD',b'currency:EUR'), raw.replace(b'2026-03-31',b'2026-03-30')):
            if bad_raw == raw: continue
            with self.assertRaises(units.FilingUnitError): self.replay(bad_raw, record)
        forged = copy.deepcopy(record); forged['supplementary_original_revenue']['rule_ref'] = 'future'
        with self.assertRaises(units.FilingUnitError): self.replay(raw, forged)

    def test_no_additions_preserve_old_context_in_normal_preflight(self):
        raw = XML.encode(); old = self.record(raw, revenue.FIXED_ZERO_RULE_REF)
        old.update(version_id='old-context', ingest_id='filing', status='available', mapping_hash='unchanged')
        old['content_hash'] = content_hash(old)
        self.c.execute('CREATE TABLE statement_context_versions(version_id TEXT,record_json TEXT,content_hash TEXT)')
        from dalton_core.store import canonical_json
        self.c.execute('INSERT INTO statement_context_versions VALUES(?,?,?)', ('old-context',canonical_json(old),old['content_hash'])); self.c.commit()
        class Missions:
            connection = self.c
            store = object()
            def statement_filings(inner, company): return [{'ingest_id':'filing', 'filed':'2026-04-01'}]
        new = self.record(raw, revenue.RULE_REF); new.update(version_id='new-context', status='available', mapping_hash='unchanged')
        with patch('dalton_core.statement_context._latest', return_value='old-context'), \
             patch('dalton_core.statement_context.prepare_context', return_value=new) as prepare, \
             patch('dalton_core.statement_context.StatementContextAuthority') as authority:
            self.assertEqual(revenue.prepare_model_revenue_inputs(Missions(),self.state,'company:any'), {'refreshed':False,'context_refs':[]})
        prepare.assert_called_once(); authority.assert_not_called()

    def test_validation_change_wakes_old_hold_without_changing_paid_task_or_repair(self):
        from dalton_core.company_model_cli import model_spec_validation_contract, TASK_HASH, REPAIR_CONTRACT_HASH
        from dalton_core.company_model_state import build_company_model_state
        from dalton_core.mission_model_spec_lane import MissionModelSpecLaneCoordinator, business_key
        from tests import test_mission_model_spec_lane as fixture
        current = model_spec_validation_contract(); prior = {**current,'original_revenue_context':revenue.FIXED_ZERO_RULE_REF}
        old_hash, new_hash = content_hash(prior), content_hash(current)
        self.assertNotEqual(old_hash, new_hash)
        self.assertEqual(TASK_HASH,'608338bcd6cf70cbe6e012acd5f579b99d34f9d990d5bc846c4059a198eda9af')
        self.assertEqual(REPAIR_CONTRACT_HASH,'3c5fa10ea1a061644a44bf9f49edc6a290ee39e34b616a86269ad4a481ec1679')
        missions = fixture.FakeMissions([fixture.IBM]); launcher = fixture.FakeLauncher()
        launcher.financial_validation_contract_hash = lambda:old_hash
        mission = {'id':'coverage-mission-version:test:1','universe':[{'company_ref':fixture.IBM,'ticker':'IBM'}]}
        lane = MissionModelSpecLaneCoordinator(missions=missions,launcher=launcher,mission=lambda:mission)
        state = build_company_model_state(missions,fixture.IBM,ticker='IBM',numeric_context_policy=launcher.numeric_context_policy(),prompt_byte_limit=launcher.prompt_byte_limit)
        key = business_key(company_ref=fixture.IBM,state_hash=state['state_hash'],task_hash=TASK_HASH,repair_policy_hash=launcher.repair_policy_hash(),validation_hash=old_hash)
        for _ in range(3): lane.budget.record(key,status='refused',reason='original revenue fact lacks exact numeric/context/unit parse')
        before = lane.budget.blocked(key).as_wire()
        self.assertEqual(lane.dispatch_once()['status'],'held')
        launcher.financial_validation_contract_hash = lambda:new_hash
        self.assertEqual(lane.dispatch_once()['status'],'launched')
        self.assertEqual(len(launcher.started),1)
        self.assertEqual(lane.budget.blocked(key).as_wire(),before)


if __name__ == '__main__': unittest.main()
