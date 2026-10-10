"""Native fetch receipts, separate Exhibit 13 originals and normal review gates."""
import copy,hashlib,json,tempfile,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from dalton_core.store import content_hash,canonical_json
from dalton_core.coverage_mission import CoverageMissionAuthority
from dalton_core.document_extraction import DocumentExtractionService
from dalton_core.connector_authority_port import ReadOnlyConnectorReceiptReader
from dalton_core.sec_annual_report_exhibit import verified_exhibit_source,AnnualReportExhibitConflict
from dalton_core.evidence_corpus import CorpusContext,Candidate,load_document,iter_candidates,CorpusSkip
from dalton_core.public_web_connector import public_web_url_ref
from tests.test_public_web_fetch_lane import FetchHarness,transport_for
from tests.test_annual_report_exhibit import INCORPORATING_10K,EXHIBIT_13
from tests.p9a_fixtures import bootstrap_method_authorities,mission_params,OWNER

ACCESSION='0001467373-26-000010'
COMPANY='company:sec-cik:0001467373'
PARENT='sec:filing:'+ACCESSION
ROOT_URL='https://www.sec.gov/Archives/edgar/data/1467373/'+ACCESSION.replace('-','')
BASE_URL=ROOT_URL+'/issuer-20251231.htm'
CHILD_URL=ROOT_URL+'/issuer-20251231_d2.htm'
TICKET='public-web-fetch:'+'a'*24

class AnnualExhibitSourceTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.root=Path(self.tmp.name)
        with patch('tests.test_public_web_fetch_lane.CITATIONS',[{'url':BASE_URL,'title':'10-K'},{'url':CHILD_URL,'title':'Exhibit 13'}]):
            self.h=FetchHarness(self.root,transport=transport_for(INCORPORATING_10K))
        self.addCleanup(self.h.close);receipt=self.h.discover()
        self.base=self.h.fetch.manifest(self.h.fetch.fetch(self.h.fetch.build_request(self.h.authority(receipt,public_web_url_ref(BASE_URL)))))
        self.h.fetch.transport=transport_for(EXHIBIT_13)
        self.child=self.h.fetch.manifest(self.h.fetch.fetch(self.h.fetch.build_request(self.h.authority(receipt,public_web_url_ref(CHILD_URL)))))
        self.directory=self.root/'fetches'/('a'*24);self.directory.mkdir(parents=True)
        self.save()
        seed=bootstrap_method_authorities(self.h.core);self.missions=CoverageMissionAuthority(self.h.core)
        params=mission_params(seed);self.mission=self.missions.create_mission(params.pop('mission_ref'),**params)
        discovery={'id':'fixture:sec-parent-discovery','schema_version':'0.1','created_at':'2026-10-09T00:00:00+00:00'}
        discovery['content_hash']=content_hash(discovery)
        # Hermetic parent queue, while both body fetches above are native Core
        # connector receipts with a fake transport; no model/network operation.
        with self.missions._transaction() as cur:
            cur.execute('INSERT INTO coverage_mission_source_discoveries VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (discovery['id'],self.mission['id'],self.mission['content_hash'],COMPANY,'source:sec-edgar','fixture:sec-plan','1'*64,
                 'annual-report-10k','2'*64,receipt['connector_invocation_ref'],receipt['source_envelope_ref'],receipt['source_envelope_hash'],OWNER,OWNER,
                 canonical_json(discovery),discovery['content_hash'],discovery['created_at']))
            cur.execute('INSERT INTO coverage_mission_discovered_documents(record_id,mission_version_ref,company_ref,source_ref,document_ref,discovery_ref,status,ticket_ref,created_at,updated_at,host) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                 ('fixture:parent',self.mission['id'],COMPANY,'source:sec-edgar',PARENT,discovery['id'],'acquired',TICKET,discovery['created_at'],discovery['created_at'],'www.sec.gov'))
        self.parent_review=self.missions.register_document_review('fixture:parent',requested_by=OWNER)
        launcher=SimpleNamespace(state_dir=self.root,spool_dir=None,read_completed_manifest=lambda *_:self.base)
        self.writer=SimpleNamespace(store=self.h.core,state_dir=self.root,coverage_mission=self.missions,
            _transcript_spool=self.h.spool,web_fetch_launcher=launcher,_connectors=self.h.connectors,
            observability=self.h.observability,_document_extraction_model_config=None)
        self.service=DocumentExtractionService(self.writer)

    def save(self):
        for name,value in [('manifest.json',self.base),('annual-report-exhibit-manifest.json',self.child),
            ('summary.json',{'status':'succeeded','url_ref':PARENT}),
            ('annual-report-exhibit.json',{'exhibit':{'status':'succeeded','exhibit_name':'issuer-20251231_d2.htm',
                'manifest_ref':self.child['id'],'manifest_hash':self.child['content_hash']}})]:
            p=self.directory/name;p.write_text(json.dumps(value));p.chmod(0o600)

    def source(self,company=COMPANY,spool=None):
        return verified_exhibit_source(self.h.core,spool or self.h.spool,self.root,ticket_ref=TICKET,
            parent_document_ref=PARENT,company_ref=company,receipt_reader=ReadOnlyConnectorReceiptReader(self.h.core.connection))

    def test_independent_original_is_indexed_and_queued_without_rekeying_parent_window(self):
        parent=self.missions.document_review(self.parent_review['review_id'])
        before=self.service.source_context(parent['review_id'],content_hash(parent),0,OWNER)
        receipt=self.missions.register_acquired_annual_attachment('fixture:parent',state_dir=self.root,spool=self.h.spool,
                receipt_reader=ReadOnlyConnectorReceiptReader(self.h.core.connection),requested_by=OWNER)
        child=self.missions.document_review(receipt['review_id'])
        context=self.service.source_context(child['review_id'],content_hash(child),0,OWNER)
        self.assertEqual(context['document_ref'],'sec:exhibit:'+ACCESSION+':issuer-20251231_d2.htm')
        self.assertEqual(context['body_sha256'],self.child['body_sha256']);self.assertEqual(context['canonical_url'],CHILD_URL)
        self.assertIn('Consulting',str(context['quotes']));self.assertNotEqual(context['source_content_hash'],before['source_content_hash'])
        self.assertEqual(self.service.source_context(parent['review_id'],content_hash(parent),0,OWNER),before)
        again=self.missions.register_acquired_annual_attachment('fixture:parent',state_dir=self.root,spool=self.h.spool,
                receipt_reader=ReadOnlyConnectorReceiptReader(self.h.core.connection),requested_by=OWNER)
        self.assertEqual(again['review_id'],receipt['review_id'])
        candidate=next(c for c in iter_candidates(self.root) if c.family=='fetch_annual_report_exhibit')
        with CorpusContext(self.root,core=self.h.core.connection) as ctx:
            ctx.spool=self.h.spool
            body=load_document(ctx,candidate)
        self.assertEqual(body.document_ref,context['document_ref']);self.assertEqual(body.text_sha256,context['source_content_hash'])
        self.assertEqual(body.filed_company_refs,[COMPANY]);self.assertEqual(body.metadata['annual_report_attachment']['parent_body_sha256'],self.base['body_sha256'])

    def test_wrong_issuer_refuses_before_queue_or_window(self):
        with self.assertRaisesRegex(AnnualReportExhibitConflict,'CIK'):self.source('company:sec-cik:0000051143')

    def test_successful_missing_cached_attachment_refuses_instead_of_returning_parent(self):
        (self.directory/'annual-report-exhibit-manifest.json').unlink()
        with self.assertRaisesRegex(AnnualReportExhibitConflict,'cannot be re-proved'):self.source()
        before=self.h.core.connection.execute('SELECT count(*) FROM coverage_mission_document_reviews').fetchone()[0]
        with self.assertRaises(AnnualReportExhibitConflict):
            self.missions.register_acquired_annual_attachment('fixture:parent',state_dir=self.root,spool=self.h.spool,
                receipt_reader=ReadOnlyConnectorReceiptReader(self.h.core.connection),requested_by=OWNER)
        self.assertEqual(before,self.h.core.connection.execute('SELECT count(*) FROM coverage_mission_document_reviews').fetchone()[0])

    def test_self_rehashed_url_in_another_issuer_directory_is_refused(self):
        from tests.test_public_web_extraction_source import remanifest
        url=CHILD_URL.replace('/1467373/','/51143/');doc='public-web-document:url-sha256:'+hashlib.sha256(url.encode()).hexdigest()+':body-sha256:'+self.child['body_sha256']
        self.child=remanifest(self.child,canonical_url=url,final_url=url,url_ref=public_web_url_ref(url),document_ref=doc,source_record_ref=doc)
        self.save()
        with self.assertRaisesRegex(AnnualReportExhibitConflict,'CIK'):self.source()

    def test_raw_bytes_tamper_and_missing_spool_are_refused(self):
        class BadSpool:
            def __init__(self,real,missing):self.real=real;self.missing=missing
            def read_object(inner,digest):
                if digest==self.child['body_sha256']:
                    if inner.missing:raise FileNotFoundError('missing source bytes')
                    return b'changed original'
                return inner.real.read_object(digest)
        for missing in (False,True):
            with self.subTest(missing=missing),self.assertRaises(Exception):self.source(spool=BadSpool(self.h.spool,missing))

    def test_manifest_cannot_borrow_another_original_fetch_receipt(self):
        from tests.test_public_web_extraction_source import remanifest
        self.child=remanifest(self.child,connector_invocation_ref=self.base['connector_invocation_ref'],connector_invocation_hash=self.base['connector_invocation_hash'])
        self.save()
        with self.assertRaisesRegex(Exception,'manifest URL ref|source envelope'):self.source()
