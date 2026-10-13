"""Registry-defined zero is a source fact, never an inferred missing value."""
import copy
import hashlib
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from dalton_core import sec_filing_units as units
from dalton_core import statement_original_revenue_context as revenue
from dalton_core import forecast_source_accuracy as accuracy
from dalton_core.store import content_hash
from tests.test_statement_original_revenue_context import XML, old_row


def original(*, issuer='12', text='—', namespace=units.FIXED_ZERO_NAMESPACE, attrs=''):
    return XML.replace('xmlns:ix=', f'xmlns:t="{namespace}" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance" xmlns:ix=').replace('>12<', '>'+issuer+'<').replace('contextRef="member"', 'contextRef="member" format="t:fixed-zero" '+attrs).replace('>60</ix:nonFraction>', '>'+text+'</ix:nonFraction>').encode()


class FixedZeroTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name)
        self.c = sqlite3.connect(self.state/'core.sqlite'); self.addCleanup(self.c.close)
        self.filing = {'cik':'12', 'report_date':'2026-03-31', 'form':'10-Q', 'accession':'synthetic', 'company_ref':'company:other', 'ingest_id':'ingest:synthetic', 'content_hash':'a'*64}
        self.source = {'record_id':'exact-original'}

    def parse(self, raw):
        return units.parse_filing_units(raw, rule_ref=units.NUMERIC_RULE_REF)

    def record(self, raw, *, revenue_rule=revenue.RULE_REF):
        with patch('dalton_core.sec_filing_units._source', return_value=(self.source,raw)):
            lines, rp = revenue.supplemental_revenue_lines(self.c,self.state,self.filing,[old_row()],rule_ref=revenue_rule)
            resolved, up = units.supplemental_unit_lines(self.c,self.state,self.filing,lines)
        return {'kind':'complete_snapshot','lines':resolved,'supplementary_original_revenue':rp,'supplementary_units':up}

    def replay(self, raw, record):
        with patch('dalton_core.sec_filing_units._source', return_value=(self.source,raw)):
            units.validate_unit_supplement(self.c,record,self.filing)
            revenue.validate_revenue_supplement(self.c,record,self.filing)

    def test_multiple_issuers_any_text_source_zero_scale_sign_and_original_hash(self):
        for issuer in ('12','77'):
            for text in ('—','', 'not a reported amount', '999'):
                for attrs in ('', 'scale="6" sign="-"'):
                    raw=original(issuer=issuer,text=text,attrs=attrs)
                    p=self.parse(raw); f=p['facts'][-1]
                    self.assertEqual(float(f['value']),0)
                    self.assertEqual(p['contexts'][f['context_ref']]['cik'],issuer.zfill(10))
                    self.assertEqual(p['raw_sha256'],hashlib.sha256(raw).hexdigest())
                    self.assertEqual(len(f['fact_sha256']),64)
                    self.assertEqual(p['units'][f['unit_ref']]['unit'],'USD')

    def test_element_scoped_namespace_not_prefix_or_local_name(self):
        raw=original().replace(b'xmlns:t="http://www.xbrl.org/inlineXBRL/transformation/2020-02-12"',b'xmlns:t="https://wrong.example"').replace(b'format="t:fixed-zero"',b'xmlns:t="http://www.xbrl.org/inlineXBRL/transformation/2020-02-12" format="t:fixed-zero"')
        self.assertEqual(float(self.parse(raw)['facts'][-1]['value']),0)
        for bad in (original(namespace='https://wrong.example'),original(namespace=units.FIXED_ZERO_NAMESPACE.replace('http:','https:')),original().replace(b'format="t:',b'format="unbound:'),original().replace(b'fixed-zero',b'unknown-zero')):
            self.assertEqual(len(self.parse(bad)['facts']),1)
            with self.assertRaisesRegex(units.FilingUnitError,'numeric/context/unit'):
                self.record(bad)

    def test_nil_missing_context_unit_bad_scale_and_sign_are_not_zero(self):
        for attrs in ('xsi:nil="true"','xsi:nil="1"'):
            raw=original(attrs=attrs)
            self.assertEqual(len(self.parse(raw)['facts']),1)
            self.assertEqual(len(self.record(raw)['lines']),2)
        for raw in (original().replace(b'contextRef="member"',b'contextRef="absent"'),original().replace(b'unitRef="u"',b'unitRef="absent"'),original(attrs='scale="oops"'),original(attrs='sign="+"')):
            self.assertFalse(any(float(f['value'])==0 for f in self.parse(raw)['facts']))
            with self.assertRaises(units.FilingUnitError):self.record(raw)

    def test_new_revenue_and_units_replay_all_source_fields_not_just_zero(self):
        raw=original(); record=self.record(raw); self.replay(raw,record)
        self.assertEqual(record['supplementary_original_revenue']['rule_ref'],revenue.RULE_REF)
        self.assertEqual(record['supplementary_units']['numeric_parse_rule_ref'],units.NUMERIC_RULE_REF)
        zero=next(r for r in record['lines']if r.get('dimension_count')==2)
        self.assertEqual(zero['value'],'0'); self.assertEqual(zero['unit'],'USD')
        for badraw in (original(text='changed raw'),original().replace(b'currency:USD',b'currency:EUR'),original().replace(b'>12<',b'>13<'),original().replace(b'2026-03-31',b'2026-03-30')):
            with self.assertRaises(units.FilingUnitError):self.replay(badraw,record)
        for field,value in (('value','1'),('dimensions',{}),('unit','EUR')):
            bad=copy.deepcopy(record); next(r for r in bad['lines']if r.get('dimension_count')==2)[field]=value
            with self.assertRaises(units.FilingUnitError):self.replay(raw,bad)
        for marker in ('unknown', units.LEGACY_RULE_REF, None):
            bad=copy.deepcopy(record);bad['supplementary_units']['numeric_parse_rule_ref']=marker
            with self.assertRaises(units.FilingUnitError):self.replay(raw,bad)

    def test_legacy_parser_full_bytes_and_legacy_revenue_proofs_unchanged(self):
        raw=original(); old=units.parse_filing_units(raw)
        self.assertEqual(old['rule_ref'],units.LEGACY_RULE_REF)
        self.assertEqual(len(old['facts']),1)
        self.assertEqual(old,units.parse_filing_units(raw,rule_ref=units.LEGACY_RULE_REF))
        for rule in (revenue.LEGACY_RULE_REF,revenue.RECONCILIATION_RULE_REF):
            raw=XML.encode(); record=self.record(raw,revenue_rule=rule)
            # Exact original v1 unit proof has no numeric parse marker.
            record['supplementary_units'].pop('numeric_parse_rule_ref')
            before=copy.deepcopy(record);self.replay(raw,record);self.assertEqual(record,before)
            self.assertEqual(record['supplementary_original_revenue']['parsed_hash'],content_hash(units.parse_filing_units(raw)))
        with self.assertRaises(units.FilingUnitError):units.parse_filing_units(raw,rule_ref='unknown')

    def test_old_material_request_accepts_new_complete_proof_without_fetch(self):
        raw=original(); record=self.record(raw)
        self.c.execute('CREATE TABLE coverage_mission_statement_filings(ingest_id TEXT,company_ref TEXT,accession TEXT,cik TEXT)')
        self.c.execute('INSERT INTO coverage_mission_statement_filings VALUES(?,?,?,?)',('ingest:synthetic','company:other','synthetic','12'));self.c.commit()
        record.update(content_hash=content_hash(record))
        request={'subject':'company:other','acceptance':{'unit_proof_rule_ref':units.LEGACY_RULE_REF,'accession':'synthetic','cik':'12','document_ref':'sec:filing:synthetic'},'need':{'doc_kinds':['sec_10q']}}
        record['supplementary_units']['source']['manifest_hash']='b'*64
        with patch('dalton_core.statement_context._latest',return_value='context:verified'),patch('dalton_core.statement_context.context_record',return_value=record):
            status=units.unit_requirement_status(self.state,request)
            self.assertTrue(status['met']);self.assertFalse(status['partial'])
            bad=copy.deepcopy(record);bad['unresolved_unit_refs']=['missing']
            bad['lines'][-1]['unit']='missing'
            with patch('dalton_core.statement_context.context_record',return_value=bad):
                self.assertFalse(units.unit_requirement_status(self.state,request)['met'])

    def test_document_set_zero_conflict_does_not_inherit_parent_value(self):
        child=original(); parent=original(text='1').replace(b' format="t:fixed-zero"',b'')
        with self.assertRaisesRegex(units.FilingUnitError,'conflicting numeric'):
            units.parse_filing_units(child,resource_raw=parent,rule_ref=units.NUMERIC_RULE_REF)

    def test_new_accuracy_doc_binds_parse_rule_old_metadata_keeps_old_replay(self):
        raw=original(); source=accuracy._document(self.c,self.state,self.filing,self.source,raw,{'us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax'})
        self.assertEqual(source['numeric_parse_rule_ref'],units.NUMERIC_RULE_REF)
        self.assertEqual(len(source['facts']),2)
        self.assertIn('0',[f['value'] for f in source['facts']])
        old=accuracy._facts(XML.encode(),units.parse_filing_units(XML.encode()),'synthetic',set(source['concepts']))
        self.assertEqual(len(old),2)

    def test_free_validation_changes_without_changing_task_or_repair(self):
        from dalton_core.company_model_cli import model_spec_validation_contract
        self.assertEqual(model_spec_validation_contract()['original_revenue_context'],revenue.RULE_REF)
        current=revenue.RULE_REF
        with patch('dalton_core.statement_original_revenue_context.RULE_REF',revenue.RECONCILIATION_RULE_REF):
            self.assertNotEqual(model_spec_validation_contract()['original_revenue_context'],current)

    def test_accuracy_new_marker_and_old_absent_marker_strong_original_replay(self):
        self.c.row_factory=sqlite3.Row
        self.c.execute('CREATE TABLE coverage_mission_statement_filings(ingest_id TEXT,company_ref TEXT,cik TEXT,accession TEXT,content_hash TEXT)')
        self.c.execute('INSERT INTO coverage_mission_statement_filings VALUES(?,?,?,?,?)',('ingest:synthetic','company:other','12','synthetic','a'*64));self.c.commit()
        for raw,legacy in ((original(),False),(XML.encode(),True)):
            stored=accuracy._document(self.c,self.state,self.filing,self.source,raw,{'us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax'})
            if legacy:stored.pop('numeric_parse_rule_ref')
            proof={'rule_ref':accuracy.RULE_REF,'company_ref':'company:other','sources':[stored],'unavailable_sources':[]};proof['content_hash']=content_hash(proof)
            record={'company_ref':'company:other','execution_contract':{'source_accuracy':proof}}
            before=copy.deepcopy(record)
            with patch('dalton_core.sec_filing_units._source',return_value=(self.source,raw)),patch('dalton_core.company_model_annual_projection.verify_statement_filing'):
                accuracy.validate_originals(self.c,record)
                self.assertEqual(record,before)
            with patch('dalton_core.sec_filing_units._source',return_value=(self.source,raw.replace(b'>100<',b'>101<'))),patch('dalton_core.company_model_annual_projection.verify_statement_filing'):
                with self.assertRaises(ValueError):accuracy.validate_originals(self.c,record)

    def test_new_numeric_validation_wakes_existing_lane_and_preserves_old_failure(self):
        from dalton_core.company_model_cli import model_spec_validation_contract,TASK_HASH
        from dalton_core.company_model_state import build_company_model_state
        from dalton_core.mission_model_spec_lane import MissionModelSpecLaneCoordinator,business_key
        from tests import test_mission_model_spec_lane as fixture
        current=model_spec_validation_contract();prior={**current,'original_revenue_context':revenue.RECONCILIATION_RULE_REF}
        old_hash,new_hash=content_hash(prior),content_hash(current)
        missions=fixture.FakeMissions([fixture.IBM]);launcher=fixture.FakeLauncher()
        launcher.financial_validation_contract_hash=lambda:old_hash
        mission={'id':'coverage-mission-version:test:1','universe':[{'company_ref':fixture.IBM,'ticker':'IBM'}]}
        lane=MissionModelSpecLaneCoordinator(missions=missions,launcher=launcher,mission=lambda:mission)
        state=build_company_model_state(missions,fixture.IBM,ticker='IBM',numeric_context_policy=launcher.numeric_context_policy(),prompt_byte_limit=launcher.prompt_byte_limit)
        identity={'company_ref':fixture.IBM,'state_hash':state['state_hash'],'task_hash':TASK_HASH,'repair_policy_hash':launcher.repair_policy_hash()}
        old_key=business_key(**identity,validation_hash=old_hash)
        for _ in range(3):lane.budget.record(old_key,status='refused',reason='original revenue fact lacks exact numeric/context/unit parse')
        before=lane.budget.blocked(old_key).as_wire()
        self.assertEqual(lane.dispatch_once()['status'],'held');self.assertEqual(launcher.started,[])
        launcher.financial_validation_contract_hash=lambda:new_hash
        self.assertEqual(lane.dispatch_once()['status'],'launched')
        self.assertEqual(len(launcher.started),1)
        self.assertEqual(launcher.started[0]['financial_validation_contract_hash'],new_hash)
        self.assertEqual(lane.budget.blocked(old_key).as_wire(),before)
