"""Current permission and exact originals survive mission carry / URL aliases."""
import copy
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from dalton_core.acquired_document_source_chain import acquired_annual_parent,completed_discovery_alias_manifest
from dalton_core.document_extraction import verified_original,resolve_reading_limits
from dalton_core.connector_authority_port import ReadOnlyConnectorReceiptReader
from dalton_core.research_verification import ResearchVerificationConflict
from dalton_core.public_web_fetch_launcher import FetchLaunchRejected
from dalton_core.store import content_hash
from tests import test_annual_report_extraction_source as annual
from tests import test_public_web_fetch_lane as fetched

class AcquiredDocumentSourceChainTests(unittest.TestCase):
    def annual_fixture(self):
        f=annual.AnnualExhibitSourceTests();f.setUp();self.addCleanup(f.doCleanups)
        return f

    def test_same_scope_original_context_is_unchanged(self):
        f=self.annual_fixture()
        receipt=f.missions.register_acquired_annual_attachment('fixture:parent',state_dir=f.root,
            spool=f.h.spool,receipt_reader=ReadOnlyConnectorReceiptReader(f.h.core.connection),requested_by=annual.OWNER)
        review=f.missions.document_review(receipt['review_id'])
        first=f.service.source_context(review['review_id'],content_hash(review),0,annual.OWNER)
        with patch('dalton_core.acquired_document_source_chain.acquired_annual_parent',wraps=acquired_annual_parent) as call:
            self.assertEqual(first,f.service.source_context(review['review_id'],content_hash(review),0,annual.OWNER))
        self.assertEqual(call.call_count,1)

    def test_carried_attachment_uses_verified_ancestor_and_still_full_raw_receipts(self):
        f=self.annual_fixture();row={'ticket_ref':annual.TICKET}
        review={'mission_version_ref':'fixture:current','company_ref':annual.COMPANY,
                'source_ref':'source:sec-edgar','document_ref':'sec:exhibit:'+annual.ACCESSION+':issuer-20251231_d2.htm'}
        chain={'fixture:current':{'mission_ref':'fixture:mission','prior_version_ref':f.mission['id']},
               f.mission['id']:{'mission_ref':'fixture:mission','prior_version_ref':None}}
        with patch.object(f.missions,'_mission_record',side_effect=lambda ref:chain[ref]):
            parent=acquired_annual_parent(f.writer,review,row,annual.PARENT)
            self.assertEqual(parent['record_id'],'fixture:parent')
            exact=verified_original(f.writer,review,row,resolve_reading_limits(None),ReadOnlyConnectorReceiptReader(f.h.core.connection))
            self.assertEqual(exact[0]['body_sha256'],f.child['body_sha256'])
            self.assertIn('Consulting',exact[1])
            f.child['body_sha256']='f'*64;f.save()
            with self.assertRaises(Exception):verified_original(f.writer,review,row,resolve_reading_limits(None),ReadOnlyConnectorReceiptReader(f.h.core.connection))

    def test_real_current_grant_carries_child_without_requiring_closed_parent_carry(self):
        f=self.annual_fixture()
        registered=f.missions.register_acquired_annual_attachment('fixture:parent',state_dir=f.root,
            spool=f.h.spool,receipt_reader=ReadOnlyConnectorReceiptReader(f.h.core.connection),requested_by=annual.OWNER)
        f.missions.resolve_document_review(f.parent_review['review_id'],resolution='dismissed',actor_ref=annual.OWNER,rationale='Fixture parent review already completed independently')
        old=f.mission
        fields=('title','objective','industry_ref','universe','research_questions','deliverables','source_plan','bindings','autonomy','budget')
        params={k:copy.deepcopy(old[k]) for k in fields}
        params['autonomy']['may_write'].append('source_discovery')
        params.update(actor_ref=annual.OWNER,version_id='coverage-mission-version:us-it-services:2',
            prior_version_ref=old['id'],idempotency_key='fixture:second-mission')
        new=f.missions.create_mission(old['mission_ref'],**params)
        carried=f.missions.carry_forward_awaiting_reviews(old['mission_ref'])
        child=next(item for item in carried if item['review_id']==registered['review_id'])
        self.assertEqual(child['status'],'carried',child)
        review=f.missions.document_review(child['carried_to']['review_id'])
        context=f.service.source_context(review['review_id'],content_hash(review),0,annual.OWNER)
        self.assertEqual(context['mission_version_ref'],new['id'])
        self.assertEqual(context['body_sha256'],f.child['body_sha256'])
        self.assertEqual(context['annual_report_attachment']['parent_document_ref'],annual.PARENT)

    def test_ancestor_cannot_change_mission_owner_or_loop(self):
        f=self.annual_fixture();review={'mission_version_ref':'new','company_ref':annual.COMPANY,'source_ref':'source:sec-edgar'}
        for chain in ({'new':{'mission_ref':'one','prior_version_ref':f.mission['id']},f.mission['id']:{'mission_ref':'foreign','prior_version_ref':None}},
                      {'new':{'mission_ref':'one','prior_version_ref':'new'}}):
            with self.subTest(chain=chain),patch.object(f.missions,'_mission_record',side_effect=lambda ref:chain[ref]),self.assertRaises(ResearchVerificationConflict):
                acquired_annual_parent(f.writer,review,{'ticket_ref':annual.TICKET},annual.PARENT)

    def test_ancestor_requires_exact_company_source_ticket_and_parent(self):
        f=self.annual_fixture();review={'mission_version_ref':'new','company_ref':annual.COMPANY,'source_ref':'source:sec-edgar'}
        chain={'new':{'mission_ref':'one','prior_version_ref':f.mission['id']},f.mission['id']:{'mission_ref':'one','prior_version_ref':None}}
        for field,value in [('company_ref','company:sec-cik:0000051143'),('source_ref','source:web-search'),('ticket_ref','public-web-fetch:'+'b'*24),('parent','sec:filing:0001467373-26-999999')]:
            r=dict(review);row={'ticket_ref':annual.TICKET};parent=annual.PARENT
            if field=='ticket_ref':row[field]=value
            elif field=='parent':parent=value
            else:r[field]=value
            with self.subTest(field=field),patch.object(f.missions,'_mission_record',side_effect=lambda ref:chain[ref]),self.assertRaises(ResearchVerificationConflict):
                acquired_annual_parent(f.writer,r,row,parent)

    def test_current_permission_denial_precedes_any_original_fallback(self):
        f=self.annual_fixture();review=f.missions.document_review(f.parent_review['review_id'])
        with patch.object(f.missions,'authorize_source_discovery',side_effect=ResearchVerificationConflict('current grant withdrawn')),patch('dalton_core.document_extraction.verified_original') as original:
            with self.assertRaisesRegex(ResearchVerificationConflict,'withdrawn'):
                f.service.source_context(review['review_id'],content_hash(review),0,annual.OWNER)
            original.assert_not_called()

    def alias_fixture(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup)
        h=fetched.FetchHarness(Path(tmp.name),transport=fetched.transport_for());self.addCleanup(h.close)
        receipt=h.discover();manifest=h.fetch.manifest(h.fetch.fetch(h.fetch.build_request(h.authority(receipt,fetched.URL_A))))
        authority={'url_ref':manifest['url_ref'],'canonical_url':manifest['canonical_url'],'host':manifest['host']}
        discovery={'company_ref':'company:sec-cik:0001467373','source_ref':'source:sec-edgar',
                   'source_envelope_ref':receipt['source_envelope_ref'],'source_envelope_hash':receipt['source_envelope_hash']}
        launcher=SimpleNamespace(state_dir=Path(tmp.name),spool_dir=None,
            locate_completed_manifest=lambda ref:(_ for _ in ()).throw(FetchLaunchRejected('no completed acquisition ticket for this document')),
            locate_completed_manifest_binding=lambda ref:{'ticket_ref':'fixture:completed','manifest':manifest})
        writer=SimpleNamespace(store=h.core,coverage_mission=SimpleNamespace(discovery_record=lambda ref:discovery),
            state_dir=Path(tmp.name),_transcript_spool=h.spool,web_fetch_launcher=launcher)
        review={'document_ref':'sec:filing:0001467373-25-000217','company_ref':discovery['company_ref'],'source_ref':discovery['source_ref']}
        return h,writer,review,{'ticket_ref':None,'discovery_ref':'fixture:discovery'},authority,discovery,manifest

    def test_exact_alias_reuses_original_receipts_and_does_not_fetch(self):
        h,w,r,row,a,d,m=self.alias_fixture()
        with patch('dalton_core.acquired_document_source_chain.url_authority_from_discovery',return_value=a) as resolve:
            original=verified_original(w,r,row,resolve_reading_limits(None),h.fetch.receipts)
        self.assertEqual(original[0],m);self.assertIn('original bytes',original[1])
        self.assertEqual(resolve.call_args.kwargs['url_ref'],r['document_ref'])
        self.assertEqual(resolve.call_args.kwargs['source_envelope_ref'],d['source_envelope_ref'])

    def test_alias_rejects_wrong_company_source_url_and_discovery_hash(self):
        for field,value in [('company_ref','company:sec-cik:0000051143'),('source_ref','source:web-search'),('source_envelope_hash','f'*64),('url_ref','public-web-url:sha256:'+'f'*64),('canonical_url','https://example.org/other'),('host','other.example.org')]:
            h,w,r,row,a,d,m=self.alias_fixture()
            if field in d:d[field]=value
            else:a[field]=value
            with self.subTest(field=field),patch('dalton_core.acquired_document_source_chain.url_authority_from_discovery',return_value=a),self.assertRaises(ResearchVerificationConflict):
                completed_discovery_alias_manifest(w,w.web_fetch_launcher,r,row,h.spool)

    def test_bad_manifest_never_uses_alias_and_alias_still_rejects_raw_tamper(self):
        h,w,r,row,a,d,m=self.alias_fixture()
        w.web_fetch_launcher.locate_completed_manifest=lambda ref:(_ for _ in ()).throw(FetchLaunchRejected('manifest hash drifted'))
        with patch('dalton_core.acquired_document_source_chain.completed_discovery_alias_manifest') as alias:
            with self.assertRaisesRegex(FetchLaunchRejected,'drifted'):verified_original(w,r,row,resolve_reading_limits(None),h.fetch.receipts)
            alias.assert_not_called()
        w.web_fetch_launcher.locate_completed_manifest=lambda ref:(_ for _ in ()).throw(FetchLaunchRejected('no completed acquisition ticket for this document'))
        with patch('dalton_core.acquired_document_source_chain.url_authority_from_discovery',return_value=a),patch.object(h.spool,'read_object',return_value=b'changed original'),self.assertRaises(Exception):
            verified_original(w,r,row,resolve_reading_limits(None),h.fetch.receipts)
