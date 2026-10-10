"""Two registered renderings may support one raw-original numeric fact."""
import copy
import hashlib
import unittest
from unittest.mock import patch
from tests import test_directed_numeric_workflow as workflow
from tests import test_directed_numeric_figure_support as support_fixture
from dalton_core.document_research import PublicWebDocumentSourceAdapter, READ_REQUEST_SCHEMA_VERSION, READ_OPERATION, DocumentResearchError
from dalton_core.directed_numeric_executor import source_binding
from dalton_core.directed_numeric_research import CONTRACT_REF, DirectedNumericError
from dalton_core.directed_numeric_figure_support import REGISTERED_RENDER_SUPPORT_CONTRACT, verify_support, figure_support
from dalton_core.store import content_hash
from dalton_core.claim_index_figures import promote_figure


class HeldRegisteredRenderingTests(unittest.TestCase):
    def fixture(self):
        h=support_fixture.NumericFigureSupportTests(methodName='runTest');self.addCleanup(h.doCleanups)
        text='<ix:header><ix:hidden><ix:nonNumeric>Hidden legacy header 777777</ix:nonNumeric></ix:hidden></ix:header>'+workflow.TEXT
        materialize=PublicWebDocumentSourceAdapter.materialize
        def old_default(adapter, **kwargs):
            return materialize(adapter,**{**kwargs,'legacy':True})
        with patch.object(workflow,'TEXT',text),patch.object(PublicWebDocumentSourceAdapter,'materialize',old_default):
            w,f,a,admission,ex,p,v=h.fixture()
        old=admission['request']['registration']
        current,current_text=a.registry.acquired_document_adapter.materialize_record(
            record_id=old['source_authority']['ref'],acquisition_ticket_ref=old['acquisition_ticket_ref'],legacy=False)
        self.assertNotEqual(current['content_hash'],old['content_hash'])
        self.assertEqual(current['raw_source'],old['raw_source'])
        # Normal read verifies both current source and the admission's closed
        # historical renderer, never a fake body supplied by a model.
        request={k:admission['request'][k] for k in ('purpose','research_question','policy_ref','policy_hash')}
        read=a.registry.read({**request,'registration':current,'schema_version':READ_REQUEST_SCHEMA_VERSION,
                             'operation':READ_OPERATION,'source_start':0,'source_end':len(current_text)})
        quote=f"quote:0:{len(current_text)}:{read['text_sha256'][:16]}"
        source=source_binding(f.store.connection,old,admission['company_ref'])
        wire={**p.candidate_wire['targets'][0]['figure'],'quote_id':quote,'citation_text':read['text']}
        a._missions.record_document_figures(company_ref=admission['company_ref'],review_ref=source['review_id'],
            document_ref=source['document_ref'],source_manifest_hash=source['source_manifest_hash'],source_grade=source['source_grade'],
            figures=[wire],observed_by=admission['actor_ref'],numeric_contract_ref=CONTRACT_REF)
        figure=a._missions.document_figures(company_ref=admission['company_ref'],metric_ref=wire['metric_ref'],limit=2000)[0]
        staged=promote_figure(f.store,ex.staging,figure=figure,actor_ref=admission['actor_ref'],idempotency_key='synthetic-new-render-figure')
        f.store.commit_policy_candidate(evidence=staged['evidence'],claim=staged['claim'],material=staged['material'],source_verification=staged['source_verification'],numeric_spec=None,numeric_verification=staged['numeric_verification'],idempotency_key='synthetic-new-render-canonical')
        return h,w,f,a,admission,ex,p,v,figure,current

    def test_newer_held_render_and_original_admission_render_both_strongly_replay(self):
        h,w,f,a,admission,ex,p,v,figure,current=self.fixture()
        old_claims=h.rows(f.store.connection,'claim_versions');old_figures=h.rows(f.store.connection,'coverage_mission_document_figures')
        done=w.drive(ex,admission);receipt=done['numeric_result'];mapping=receipt['figure_supports'][0]
        self.assertEqual(done['research_status'],'numeric_targets_completed')
        self.assertEqual(receipt['figure_support_contract'],REGISTERED_RENDER_SUPPORT_CONTRACT)
        self.assertEqual(mapping['document_version_hash'],admission['request']['registration']['content_hash'])
        self.assertEqual(mapping['held_document_version_hash'],current['content_hash'])
        self.assertNotEqual(mapping['held_document_version_hash'],mapping['document_version_hash'])
        self.assertEqual(mapping['held_registration_ref'],current['id'])
        self.assertEqual(mapping['held_raw_source_hash'],content_hash(current['raw_source']))
        self.assertTrue(all(x in h.rows(f.store.connection,'claim_versions') for x in old_claims))
        self.assertTrue(all(x in h.rows(f.store.connection,'coverage_mission_document_figures') for x in old_figures))
        self.assertEqual(ex.run_once(admission['id']),done)
        self.assertEqual((p.calls,v.calls),(1,1))

    def test_registered_version_and_raw_source_proof_tampering_refuses(self):
        h,w,f,a,admission,ex,p,v,figure,current=self.fixture()
        receipt=w.drive(ex,admission)['numeric_result'];args=h.support_args(ex,admission,receipt)
        for field in ('held_document_version_hash','held_registration_ref','held_raw_source_hash','held_read_proof_hash'):
            with self.subTest(field=field):
                bad=copy.deepcopy(receipt['figure_supports'][0]);bad[field]='f'*64
                bad['content_hash']=content_hash({k:x for k,x in bad.items() if k!='content_hash'})
                with self.assertRaisesRegex(DirectedNumericError,'mapping does not replay'):verify_support(bad,**args)

    def test_alternate_representation_cannot_bind_other_original_or_company(self):
        h,w,f,a,admission,ex,p,v,figure,current=self.fixture()
        receipt=w.drive(ex,admission)['numeric_result'];args=h.support_args(ex,admission,receipt)
        adapter=a.registry.acquired_document_adapter;materialize=adapter.materialize_record
        def changed(**kwargs):
            registration,text=materialize(**kwargs)
            if not kwargs.get('legacy'):
                registration=copy.deepcopy(registration);registration['source_authority']['company_ref']='company:other'
                registration['id']='registered-document:sha256:'+content_hash({k:x for k,x in registration.items() if k not in {'id','content_hash'}})
                registration['content_hash']=content_hash({k:x for k,x in registration.items() if k!='content_hash'})
            return registration,text
        with patch.object(adapter,'materialize_record',side_effect=changed),self.assertRaisesRegex(DirectedNumericError,'different original'):
            figure_support(**args)

    def test_unavailable_original_does_not_fall_back_to_claimed_representation(self):
        h,w,f,a,admission,ex,p,v,figure,current=self.fixture()
        receipt=w.drive(ex,admission)['numeric_result'];args=h.support_args(ex,admission,receipt)
        with patch.object(a.registry,'read',side_effect=RuntimeError('original proof unavailable')):
            with self.assertRaisesRegex(RuntimeError,'original proof unavailable'):figure_support(**args)
        self.assertEqual((p.calls,v.calls),(1,1))

    def test_out_of_bounds_original_read_remains_a_strict_refusal(self):
        h,w,f,a,admission,ex,p,v,figure,current=self.fixture()
        from dalton_core.directed_numeric_figure_support import _held_registered_read, _QUOTE
        request={k:admission['request'][k] for k in ('purpose','research_question','registration','policy_ref','policy_hash')}
        match=_QUOTE.fullmatch(figure['quote_id'])
        request.update(schema_version=READ_REQUEST_SCHEMA_VERSION,operation=READ_OPERATION,
                       source_start=int(match.group(1)),source_end=int(match.group(2)))
        with patch.object(a.registry,'read',side_effect=DocumentResearchError('document read range exceeds the registered text')), \
             patch.object(a.registry.acquired_document_adapter,'materialize_record',side_effect=AssertionError('no fallback after failed original read')):
            with self.assertRaisesRegex(DocumentResearchError,'range exceeds'):
                _held_registered_read(a.registry,request,figure,match)
