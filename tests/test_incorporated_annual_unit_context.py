"""An incorporated inline document set needs both exact acquired originals."""
import copy
import hashlib
import sqlite3
import re
import unittest
from types import SimpleNamespace
from unittest.mock import patch
from dalton_core import sec_filing_units as units
from dalton_core.store import content_hash
from tests import test_sec_filing_unit_context as unit_fixture
from tests import test_annual_report_extraction_source as annual_fixture


def originals(*, duplicate_unit=False, context_cik='1467373', eps='9.04', incorporated=True):
    primary = unit_fixture.XML.replace('>12</x:identifier>', '>'+context_cik+'</x:identifier>')
    incorporation = annual_fixture.INCORPORATING_10K.decode().split('<body>', 1)[1].split('</body>', 1)[0]
    if not incorporated:
        incorporation = incorporation.replace('are incorporated by reference', 'are not included').replace('Documents incorporated by reference:', 'Other references:')
    primary = primary.replace('</body>', incorporation+'</body>')
    child = primary.split('<body>', 1)[0]+'<body><p>Issuer annual report</p>'
    if duplicate_unit:
        child += '<x:unit id="opaque-eps"><x:measure>x:pure</x:measure></x:unit>'
    child += '<ix:nonFraction name="g:EarningsPerShareDiluted" contextRef="c" unitRef="opaque-eps">'+eps+'</ix:nonFraction></body></html>'
    return primary.encode(), child.encode()


def eps_row(value='9.04'):
    return unit_fixture.row('g:EarningsPerShareDiluted', 'opaque-eps', value)


class IncorporatedAnnualUnits(unittest.TestCase):
    def fixture(self, **changes):
        parent, child = originals(**changes)
        with patch.object(annual_fixture, 'INCORPORATING_10K', parent), patch.object(annual_fixture, 'EXHIBIT_13', child):
            fixture = annual_fixture.AnnualExhibitSourceTests()
            fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        header = {'form':'10-K', 'filing_date':'2026-02-24', 'primary_document':'issuer-20251231.htm'}
        for p in (patch('dalton_core.acquired_sec_filings.SPOOL_ROOTS', ('spool',)),
                  patch('dalton_core.acquired_sec_filings._manifest_reader', return_value=SimpleNamespace(read_completed_manifest=lambda *_:fixture.base)),
                  patch('dalton_core.acquired_sec_filings._submissions_row', return_value=header)):
            p.start();self.addCleanup(p.stop)
        filing={'form':'10-K', 'filed':'2026-02-24', 'company_ref':annual_fixture.COMPANY,
                'cik':'1467373', 'accession':annual_fixture.ACCESSION}
        return fixture, filing, parent, child

    def supplement(self, fixture, filing):
        rows=[unit_fixture.row(), eps_row()]
        lines, proof=units.supplemental_unit_lines(fixture.h.core.connection, fixture.root, filing, rows)
        return {'kind':'complete_snapshot','lines':lines,'supplementary_units':proof}

    def test_two_originals_resolve_primary_resources_and_replay_both_strong_receipts(self):
        f, filing, parent, child=self.fixture()
        record=self.supplement(f, filing)
        self.assertEqual([r['unit'] for r in record['lines']], ['USD','USDPerShare'])
        proof=record['supplementary_units'];source=proof['source']
        self.assertEqual(source['document_set_rule_ref'], units.DOCUMENT_SET_RULE_REF)
        self.assertEqual(source['primary']['manifest_hash'], f.base['content_hash'])
        self.assertEqual(source['annual_report_attachment']['manifest_hash'], f.child['content_hash'])
        evidence=proof['evidence'][1]
        self.assertEqual(evidence['fact_document_sha256'],hashlib.sha256(child).hexdigest())
        self.assertEqual(evidence['context_document_sha256'],hashlib.sha256(parent).hexdigest())
        self.assertEqual(evidence['unit_document_sha256'],hashlib.sha256(parent).hexdigest())
        self.assertEqual(evidence['value'],'9.04')
        units.validate_unit_supplement(f.h.core.connection,record,filing)

    def test_missing_attachment_keeps_primary_evidence_and_exact_unresolved_gap(self):
        f, filing, _, _=self.fixture()
        (f.directory/'annual-report-exhibit-manifest.json').unlink()
        record=self.supplement(f,filing)
        self.assertEqual(record['lines'][0]['unit'],'USD')
        self.assertEqual(record['lines'][1]['unit'],'opaque-eps')
        self.assertNotIn('document_set_rule_ref',record['supplementary_units']['source'])
        units.validate_unit_supplement(f.h.core.connection,record,filing)

    def test_resource_conflict_cannot_supply_new_authority(self):
        f, filing, parent, child=self.fixture(duplicate_unit=True)
        with self.assertRaisesRegex(units.FilingUnitError,'conflicting unit'):
            units.parse_filing_units(child,resource_raw=parent)
        record=self.supplement(f,filing)
        self.assertEqual(record['lines'][1]['unit'],'opaque-eps')

    def test_same_fact_different_values_in_documents_refuses(self):
        parent,child=originals()
        fact=b'<ix:nonFraction name="g:EarningsPerShareDiluted" contextRef="c" unitRef="opaque-eps">8.04</ix:nonFraction>'
        parent=parent.replace(b'</body>',fact+b'</body>')
        with self.assertRaisesRegex(units.FilingUnitError,'conflicting numeric fact'):
            units.parse_filing_units(child,resource_raw=parent)

    def test_wrong_issuer_accession_or_attachment_binding_refuses_frozen_replay(self):
        f,filing,_,_=self.fixture();record=self.supplement(f,filing)
        for change in ({'cik':'51143'},{'accession':'0001467373-25-000001'},{'company_ref':'company:sec-cik:0000051143'}):
            with self.subTest(change=change),self.assertRaises(ValueError):
                units.validate_unit_supplement(f.h.core.connection,record,{**filing,**change})
        bad=copy.deepcopy(record);bad['supplementary_units']['source']['annual_report_attachment']['body_sha256']='0'*64
        with self.assertRaises(ValueError):units.validate_unit_supplement(f.h.core.connection,bad,filing)

    def test_receipt_missing_or_original_drift_refuses_frozen_replay(self):
        f,filing,_,_=self.fixture();record=self.supplement(f,filing)
        (f.directory/'annual-report-exhibit-manifest.json').unlink()
        with self.assertRaises(ValueError):units.validate_unit_supplement(f.h.core.connection,record,filing)
        f.save()
        digest=f.child['body_sha256']
        raw_file=f.h.spool._objects/digest[:2]/digest
        raw_file.chmod(0o600);raw_file.write_bytes(b'changed original')
        with self.assertRaises(ValueError):units.validate_unit_supplement(f.h.core.connection,record,filing)

    def test_wrong_context_or_value_never_inherits_unit(self):
        primary,child=originals(context_cik='51143')
        parsed=units.parse_filing_units(child,resource_raw=primary)
        self.assertEqual(units.match_row_units(parsed,[eps_row()],cik='1467373')[1],[])
        primary,child=originals();parsed=units.parse_filing_units(child,resource_raw=primary)
        self.assertEqual(units.match_row_units(parsed,[eps_row('9.05')],cik='1467373')[1],[])

    def test_legacy_single_document_shape_and_hash_stay_identical(self):
        old=unit_fixture.XML.encode()
        expected={'raw_sha256':hashlib.sha256(old).hexdigest(),'rule_ref':units.RULE_REF}
        parsed=units.parse_filing_units(old)
        self.assertEqual({k:parsed[k] for k in expected},expected)
        self.assertEqual(set(parsed),{'rule_ref','raw_sha256','namespace_bindings','units','contexts','facts'})
        self.assertNotIn('document_set_rule_ref',parsed)
        lines,proof=units.match_row_units(parsed,[unit_fixture.row()],cik='12')
        self.assertEqual(set(proof[0]),{'ordinal','concept','period_start','period_end','value','dimensions','source_unit_ref','unit','definition_sha256','context_ref','context_sha256','fact_sha256','content_hash'})
        self.assertNotIn('fact_document_sha256',proof[0])
        self.assertEqual(proof[0]['content_hash'],'6004e25aca5d350b841e85153baf7258adde698ee6db818ba18976b9cc73b719')

    def test_typed_material_acceptance_retains_both_source_bindings(self):
        f,filing,_,_=self.fixture();record=self.supplement(f,filing);record['content_hash']=content_hash(record)
        c=f.h.core.connection
        # Only the context lookup is replaced; its returned record has already
        # replayed through the real connector receipt and raw-source verifier.
        request={'subject':filing['company_ref'],'need':{'doc_kinds':['sec_10k']},'acceptance':{
            'unit_proof_rule_ref':units.RULE_REF,'accession':filing['accession'],'cik':filing['cik'],
            'document_ref':annual_fixture.PARENT}}
        state=f.root/'acceptance';state.mkdir()
        with sqlite3.connect(state/'core.sqlite') as acceptance_db:
            acceptance_db.execute('CREATE TABLE coverage_mission_statement_filings(ingest_id,company_ref,cik,accession)')
            acceptance_db.execute('INSERT INTO coverage_mission_statement_filings VALUES(?,?,?,?)',
                ('fixture:filing',filing['company_ref'],filing['cik'],filing['accession']))
        acceptance_db.close()
        def strong_reader(_connection,ref):
            self.assertEqual(ref,'context:fixture')
            units.validate_unit_supplement(c,record,filing)
            return record
        from dalton_core.material_acquisition import evaluate_acceptance
        request['need']['kind']='document'
        with patch('dalton_core.statement_context._latest',return_value='context:fixture'),patch('dalton_core.statement_context.context_record',side_effect=strong_reader):
            status=evaluate_acceptance(state,request,identity={})
            (f.directory/'annual-report-exhibit-manifest.json').unlink()
            self.assertFalse(evaluate_acceptance(state,request,identity={})['met'])
        self.assertTrue(status['met'])
        item=status['items'][0]
        self.assertEqual(item['source_manifest_hash'],f.base['content_hash'])
        self.assertEqual(item['raw_sha256'],f.base['body_sha256'])
        self.assertEqual(item['primary_raw_sha256'],f.base['body_sha256'])
        self.assertEqual(item['annual_report_attachment_raw_sha256'],f.child['body_sha256'])
        self.assertEqual(item['annual_report_attachment_manifest_hash'],f.child['content_hash'])
        self.assertEqual(item['annual_report_attachment_hash'],record['supplementary_units']['source']['annual_report_attachment']['content_hash'])

    def test_duplicate_context_definition_refuses_document_set(self):
        primary,child=originals()
        context=re.search(rb'<x:context.*?</x:context>',primary).group()
        child=child.replace(b'<body>',b'<body>'+context.replace(b'>1467373<',b'>51143<'))
        with self.assertRaisesRegex(units.FilingUnitError,'conflicting context'):
            units.parse_filing_units(child,resource_raw=primary)

    def test_acquired_attachment_without_explicit_incorporation_still_has_no_unit_authority(self):
        f,filing,_,_=self.fixture(incorporated=False)
        record=self.supplement(f,filing)
        self.assertEqual(record['lines'][1]['unit'],'opaque-eps')
        self.assertNotIn('document_set_rule_ref',record['supplementary_units']['source'])

    def test_rehashed_numeric_proof_cannot_change_original_financial_fact(self):
        f,filing,_,_=self.fixture();record=self.supplement(f,filing)
        bad=copy.deepcopy(record);bad['lines'][1]['value']='9.05'
        evidence=bad['supplementary_units']['evidence'][1];evidence['value']='9.05'
        evidence['content_hash']=content_hash({k:v for k,v in evidence.items()if k!='content_hash'})
        bad['lines'][1]['unit_definition_hash']=evidence['content_hash']
        bad['supplementary_units']['evidence_hash']=content_hash(bad['supplementary_units']['evidence'])
        with self.assertRaisesRegex(units.FilingUnitError,'replay differs'):
            units.validate_unit_supplement(f.h.core.connection,bad,filing)

    def test_unit_validation_revision_wakes_normal_lane_without_clearing_old_hold(self):
        from dalton_core.company_model_cli import model_spec_validation_contract,TASK_HASH
        from dalton_core.company_model_launcher import CompanyModelSpecLauncher
        from dalton_core.company_model_state import build_company_model_state
        from dalton_core.mission_model_spec_lane import MissionModelSpecLaneCoordinator,business_key
        from tests import test_mission_model_spec_lane as lane_fixture
        current=model_spec_validation_contract()
        self.assertEqual(current['exact_filing_unit_document_set'],units.DOCUMENT_SET_RULE_REF)
        old={k:v for k,v in current.items()if k!='exact_filing_unit_document_set'}
        old_hash,new_hash=content_hash(old),content_hash(current)
        missions=lane_fixture.FakeMissions([lane_fixture.IBM]);launcher=lane_fixture.FakeLauncher()
        launcher.financial_validation_contract_hash=lambda:old_hash
        mission={'id':'coverage-mission-version:test:1','universe':[{'company_ref':lane_fixture.IBM,'ticker':'IBM'}]}
        lane=MissionModelSpecLaneCoordinator(missions=missions,launcher=launcher,mission=lambda:mission)
        state=build_company_model_state(missions,lane_fixture.IBM,ticker='IBM',numeric_context_policy=launcher.numeric_context_policy(),prompt_byte_limit=launcher.prompt_byte_limit)
        identity={'company_ref':lane_fixture.IBM,'state_hash':state['state_hash'],'task_hash':TASK_HASH,'repair_policy_hash':launcher.repair_policy_hash()}
        old_key=business_key(**identity,validation_hash=old_hash)
        for _ in range(3):lane.budget.record(old_key,status='refused',reason='exact original unit proof unavailable')
        old_failure=lane.budget.blocked(old_key).as_wire()
        self.assertEqual(lane.dispatch_once()['status'],'held')
        self.assertEqual(launcher.started,[])
        launcher.financial_validation_contract_hash=lambda:new_hash
        result=lane.dispatch_once()
        self.assertEqual(result['status'],'launched')
        self.assertEqual(len(launcher.started),1)
        self.assertEqual(launcher.started[0]['financial_validation_contract_hash'],new_hash)
        self.assertEqual(lane.budget.blocked(old_key).as_wire(),old_failure)
        actual=object.__new__(CompanyModelSpecLauncher)
        self.assertNotEqual(actual._ticket_digest(**identity,validation_hash=old_hash),actual._ticket_digest(**identity,validation_hash=new_hash))
        self.assertEqual(actual._ticket_digest(**identity,validation_hash=new_hash),actual._ticket_digest(**identity,validation_hash=new_hash))
