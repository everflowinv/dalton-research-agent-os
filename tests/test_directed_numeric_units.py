from __future__ import annotations
import tempfile,unittest,json
from pathlib import Path
from unittest.mock import patch
from dalton_core.document_numeric_claim import NumericCandidateError,ALLOWED_UNITS,verify_numeric_candidate,verify_numeric_candidates
from dalton_core.document_numeric_unit_contracts import DIRECTED_NUMERIC_CONTRACT_REF as CONTRACT,DIRECTED_FIGURE_VERIFIER_REF,LEGACY_FIGURE_VERIFIER_REF,numeric_units
from dalton_core.document_numeric_extraction import TASK_HASH,OUTPUT_SCHEMA
from dalton_core.coverage_mission import CoverageMissionAuthority,CoverageMissionValidationError
from dalton_core.claim_index_figures import DocumentFigureResolver,FigureAdmissionError
from dalton_core.research_verification import ResearchVerificationConflict
from dalton_core.document_figure_grade import FILED
from dalton_core.store import DaltonStore,content_hash
Q='quote:0:120:abcdef0123456789'
QUOTE='SyntheticCo weighted-average remaining lease term was 4.1 years in FY2026.'
def candidate(**kw):
    x=dict(quote_id=Q,metric_ref='metric:weighted-average-remaining-lease-term',subject_as_named='SyntheticCo',as_reported_label='weighted-average remaining lease term',value='4.1',unit='years',currency=None,period='FY2026',basis='gaap-reported',scale=None);x.update(kw);return x

class UnitContractTests(unittest.TestCase):
    def test_old_contract_bytes_and_default_enum_stay_frozen(self):
        self.assertEqual(TASK_HASH,'a6c0c1ca01aaaef004a79fcf8ba2a54e431e9f876e68281ed6e714cad3b04a4f')
        self.assertEqual(content_hash(OUTPUT_SCHEMA),'dd1387afc3745499052046aeb8d2c70e91dfe6b669ddf019b2c58482c296ab73')
        self.assertEqual(ALLOWED_UNITS,numeric_units());self.assertNotIn('years',ALLOWED_UNITS)
        with self.assertRaises(NumericCandidateError):verify_numeric_candidate(candidate(),{Q:QUOTE})
    def test_old_verified_wire_hash_has_no_contract_field(self):
        x=candidate(unit='ratio');a=verify_numeric_candidate(x,{Q:QUOTE});b=verify_numeric_candidate(x,{Q:QUOTE},numeric_contract_ref=None)
        self.assertEqual(a,b);self.assertNotIn('numeric_contract_ref',a)
        self.assertEqual(a['content_hash'],content_hash({k:v for k,v in a.items() if k!='content_hash'}))
    def test_new_contract_keeps_exact_years_not_days(self):
        x=verify_numeric_candidate(candidate(value='4.10'),{Q:QUOTE},numeric_contract_ref=CONTRACT)
        self.assertEqual((x['value'],x['unit'],x['scale']),('4.10','years',None));self.assertEqual(x['numeric_contract_ref'],CONTRACT)
        self.assertEqual(x['content_hash'],content_hash({k:v for k,v in x.items() if k!='content_hash'}))
    def test_years_must_qualify_that_quantity(self):
        for quote in [QUOTE.replace('years','days'),QUOTE.replace('4.1 years','4.1; investments were held for 5 years'),QUOTE.replace('4.1 years','1496.5 days'),QUOTE.replace('4.1 years','a few years')]:
            with self.subTest(quote=quote),self.assertRaises(NumericCandidateError):verify_numeric_candidate(candidate(),{Q:quote},numeric_contract_ref=CONTRACT)
    def test_unknown_contract_currency_and_scale_refused(self):
        for kwargs in [{'numeric_contract_ref':'directed-document-numeric:unregistered'},{'numeric_contract_ref':CONTRACT}]:
            if kwargs['numeric_contract_ref']!=CONTRACT:
                with self.assertRaises(NumericCandidateError):verify_numeric_candidate(candidate(),{Q:QUOTE},**kwargs)
        for kw in [{'currency':'USD'},{'scale':'million'}]:
            with self.assertRaises(NumericCandidateError):verify_numeric_candidate(candidate(**kw),{Q:QUOTE},numeric_contract_ref=CONTRACT)
    def test_split_refusals_keeps_quote_truth(self):
        good,bad=verify_numeric_candidates([candidate(),candidate(value='4.2')],{Q:QUOTE},numeric_contract_ref=CONTRACT)
        self.assertEqual(len(good),1);self.assertEqual(len(bad),1)

class FigureReplayTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.store=DaltonStore(Path(self.tmp.name)/'core.sqlite');self.addCleanup(self.store.close);self.a=CoverageMissionAuthority(self.store)
    def record(self,*,contract=None,body=None):
        return self.a.record_document_figures(company_ref='company:sec-cik:0000000001',review_ref='mission-document-review:synthetic',document_ref='sec:filing:synthetic',source_manifest_hash='a'*64,source_grade=FILED,figures=[{**(body or candidate()),'citation_text':QUOTE}],observed_by='automation:synthetic',numeric_contract_ref=contract)
    def test_real_record_and_strong_figure_replay_duplicate(self):
        self.assertEqual(self.record(contract=CONTRACT)['recorded'],[candidate()['metric_ref']]);held=self.a.document_figures('company:sec-cik:0000000001')[0]
        self.assertEqual(held['verified_by'],DIRECTED_FIGURE_VERIFIER_REF);self.assertEqual(held['unit'],'years')
        row,bundle=DocumentFigureResolver(self.store.connection).verify_figure(held);self.assertEqual(row,held);self.assertEqual(bundle['verdict'],'pass')
        self.assertEqual(self.record(contract=CONTRACT)['duplicates'],[candidate()['metric_ref']])
    def test_writer_does_not_trust_claimed_contract_in_body(self):
        with self.assertRaises(CoverageMissionValidationError):self.record(body={**candidate(),'numeric_contract_ref':CONTRACT})
        self.assertEqual(self.a.document_figures('company:sec-cik:0000000001'),[])
    def test_replay_uses_held_marker_and_hash_not_callers_override(self):
        self.record(contract=CONTRACT);held=self.a.document_figures('company:sec-cik:0000000001')[0];r=DocumentFigureResolver(self.store.connection)
        for patchbody in [{'verified_by':LEGACY_FIGURE_VERIFIER_REF},{'unit':'days'},{'value':'4.2'},{'numeric_contract_ref':CONTRACT}]:
            with self.subTest(body=patchbody),self.assertRaises(ResearchVerificationConflict):r.verify_figure({**held,**patchbody})
    def test_existing_fact_cannot_silently_change_units_or_satisfy_years(self):
        self.record(body=candidate(unit='ratio'))
        with self.assertRaisesRegex(CoverageMissionValidationError,'different units'):
            self.record(contract=CONTRACT)
        held=self.a.document_figures('company:sec-cik:0000000001');self.assertEqual(len(held),1)
        self.assertEqual(held[0]['unit'],'ratio')

    def test_legacy_record_shape_hash_and_verifier_unchanged(self):
        fixed='2026-10-10T00:00:00.000000+00:00'
        with patch('dalton_core.coverage_mission._now',return_value=fixed):self.record(body=candidate(unit='ratio'))
        held=self.a.document_figures('company:sec-cik:0000000001')[0]
        self.assertEqual(held['verified_by'],LEGACY_FIGURE_VERIFIER_REF);self.assertNotIn('numeric_contract_ref',held)
        self.assertEqual(held['content_hash'],content_hash({k:v for k,v in held.items() if k!='content_hash'}))
        self.assertEqual(DocumentFigureResolver(self.store.connection).verify_figure(held)[1]['verdict'],'pass')

class DirectedYearsAutoCommitTests(unittest.TestCase):
    def test_exact_source_years_replays_through_signed_auto_commit(self):
        from tests.test_figure_admission import FigureHarness
        from tests.test_mission_source_discovery import ACN,NEW_DOC
        from tests.test_quantitative_claim_admission import VerifiedFigureAdmissionTests,ACTOR
        from dalton_core.claim_index_figures import MissionFigureAuthorityResolver,promote_figure
        from dalton_core.research_auto_commit import MISSION_VERIFIED_FIGURE_RULE_REF,ResearchAutoCommitRejected
        from types import SimpleNamespace
        source='HERMETIC FIXTURE ONLY. Accenture weighted-average remaining lease term was 4.1 years in FY2026.\n'
        with patch('tests.test_document_extraction.ORIGINAL',source):h=FigureHarness()
        self.addCleanup(h.close);staging=h.staging();self.addCleanup(staging.close)
        context=h.extraction.context();quote=context['quotes'][0]
        h.missions.record_document_figures(company_ref=ACN,review_ref=h.review['review_id'],document_ref=NEW_DOC,
            source_manifest_hash=h.extraction.manifest['content_hash'],source_grade=FILED,
            figures=[{**candidate(quote_id=quote['quote_id'],subject_as_named='Accenture'),'citation_text':quote['raw_text']}],
            observed_by=ACTOR,numeric_contract_ref=CONTRACT)
        resolver=MissionFigureAuthorityResolver(h.core.connection);held=resolver.figures(company_ref=ACN)[0]
        result=promote_figure(h.core,staging,figure=held,actor_ref=ACTOR,idempotency_key='synthetic-years-promotion')
        self.assertEqual((result['claim']['value'],result['claim']['unit']),('4.1','years'))
        def commit():return h.core.commit_policy_candidate(evidence=result['evidence'],claim=result['claim'],material=result['material'],source_verification=result['source_verification'],numeric_verification=result['numeric_verification'],idempotency_key='synthetic-years-policy-commit')
        with self.assertRaises(ResearchAutoCommitRejected):commit()
        self.assertEqual(h.core.connection.execute('SELECT COUNT(*) FROM claim_versions').fetchone()[0],0)
        VerifiedFigureAdmissionTests.sign(SimpleNamespace(core=h.core),MISSION_VERIFIED_FIGURE_RULE_REF)
        committed=commit();self.assertEqual(committed['status'],'fresh');self.assertEqual(commit()['status'],'duplicate')
        record=json.loads(h.core.connection.execute('SELECT claim_json FROM claim_versions').fetchone()[0])
        self.assertEqual((record['value'],record['unit'],record['scale']),('4.1','years','one'))

if __name__=='__main__':unittest.main()
