"""Verified measure collisions are bounded pending feedback, never answers."""
import copy
import hashlib
import json
import unittest
from unittest.mock import patch
from tests import test_directed_numeric_figure_support as support_fixture
from dalton_core.directed_numeric_executor import source_binding, numeric_feedback, read_numeric_result
from dalton_core.directed_numeric_figure_support import MEASURE_CONFLICT_CONTRACT, MEASURE_CONFLICT_REASON, NumericMeasureIdentityConflict
from dalton_core.directed_numeric_research import CONTRACT_REF, DirectedNumericError
from dalton_core.document_research import READ_REQUEST_SCHEMA_VERSION, READ_OPERATION
from dalton_core.directed_numeric_output import quotes
from dalton_core.claim_index_figures import MissionFigureAuthorityResolver, promote_figure
from dalton_core.store import canonical_json, content_hash
from dalton_core.planner_numeric_requirements import numeric_waiting_targets


class NumericMeasureConflictTests(unittest.TestCase):
    def fixture(self):
        h=support_fixture.NumericFigureSupportTests(methodName='runTest')
        self.addCleanup(h.doCleanups)
        w,f,a,admission,ex,p,v=h.fixture()
        return h,w,f,a,admission,ex,p,v

    def seed_collision(self,h,f,a,admission,ex,p,*,same_amount=False,same_quote=False):
        proof=a.registry.search(admission['request'])
        if same_quote:
            quote,text=next(iter(quotes(proof).items()))
        else:
            registration=admission['request']['registration']
            request={k:proof['request'][k] for k in ('purpose','research_question','registration','policy_ref','policy_hash')}
            end=registration['normalized_text']['characters']
            request.update(schema_version=READ_REQUEST_SCHEMA_VERSION,operation=READ_OPERATION,source_start=1,source_end=end)
            old=a.registry.read(request);text=old['text'];quote=f"quote:1:{end}:{old['text_sha256'][:16]}"
        label='Other commitments' if same_amount else 'Uncommenced leases'
        value='12.3' if same_amount else '2.5'
        wire={**p.candidate_wire['targets'][0]['figure'],'quote_id':quote,'citation_text':text,
              'as_reported_label':label,'value':value}
        source=source_binding(f.store.connection,admission['request']['registration'],admission['company_ref'])
        a._missions.record_document_figures(company_ref=admission['company_ref'],review_ref=source['review_id'],
            document_ref=source['document_ref'],source_manifest_hash=source['source_manifest_hash'],source_grade=source['source_grade'],
            figures=[wire],observed_by=admission['actor_ref'],numeric_contract_ref=CONTRACT_REF)
        figure=a._missions.document_figures(company_ref=admission['company_ref'],metric_ref=wire['metric_ref'],limit=2000)[0]
        bundle=promote_figure(f.store,ex.staging,figure=figure,actor_ref=admission['actor_ref'],idempotency_key='old-collision-stage')
        f.store.commit_policy_candidate(evidence=bundle['evidence'],claim=bundle['claim'],material=bundle['material'],
            source_verification=bundle['source_verification'],numeric_spec=None,numeric_verification=bundle['numeric_verification'],
            idempotency_key='old-collision-canonical')
        return figure

    def test_two_proved_measures_stay_separate_and_other_targets_complete_without_material_purchase(self):
        h,w,f,a,admission,ex,p,v=self.fixture()
        old=self.seed_collision(h,f,a,admission,ex,p)
        claims=h.rows(f.store.connection,'claim_versions');evidence=h.rows(f.store.connection,'evidence_versions')
        done=w.drive(ex,admission);receipt=done['numeric_result']
        self.assertEqual(done['research_status'],'numeric_waiting_evidence')
        self.assertEqual(len(receipt['completed_targets']),2)
        self.assertEqual(len(receipt['canonical_claims']),2)
        self.assertEqual(receipt['measure_conflict_contract'],MEASURE_CONFLICT_CONTRACT)
        pending=receipt['pending_targets'][0]
        self.assertEqual(pending['target_ref'],'target:rpo')
        self.assertEqual(pending['reason'],MEASURE_CONFLICT_REASON)
        proof=pending['measure_conflict_proof']
        self.assertEqual(proof['held_fact']['value'],'2.5')
        self.assertEqual(proof['verified_answer']['value'],'12.3')
        self.assertEqual(set(proof['differing_fields']),{'value','as_reported_label'})
        self.assertEqual(proof['content_hash'],content_hash({k:x for k,x in proof.items() if k!='content_hash'}))
        self.assertEqual(receipt['material_requests'],[])
        self.assertEqual(MissionFigureAuthorityResolver(f.store.connection).figure(old['figure_id']),old)
        self.assertTrue(all(row in h.rows(f.store.connection,'claim_versions') for row in claims))
        self.assertTrue(all(row in h.rows(f.store.connection,'evidence_versions') for row in evidence))
        self.assertEqual(done,ex.run_once(admission['id']))
        self.assertEqual((p.calls,v.calls),(1,1))
        feedback=numeric_feedback(f.store.connection,admission['mission_version_ref'])
        waiting=numeric_waiting_targets(feedback)['targets']
        self.assertEqual(waiting[0]['reason'],MEASURE_CONFLICT_REASON)
        self.assertLessEqual(len(MEASURE_CONFLICT_REASON),180)
        self.assertFalse(feedback[0].get('target_revision_candidate',False))

    def test_equal_amount_does_not_merge_different_reported_labels(self):
        h,w,f,a,admission,ex,p,v=self.fixture()
        self.seed_collision(h,f,a,admission,ex,p,same_amount=True)
        done=w.drive(ex,admission)
        proof=done['numeric_result']['pending_targets'][0]['measure_conflict_proof']
        self.assertEqual(proof['held_fact']['value'],proof['verified_answer']['value'])
        self.assertEqual(proof['differing_fields'],['as_reported_label'])
        self.assertEqual(done['research_status'],'numeric_waiting_evidence')

    def test_same_window_collision_is_pending_not_silently_admitted_or_merged(self):
        h,w,f,a,admission,ex,p,v=self.fixture()
        self.seed_collision(h,f,a,admission,ex,p,same_quote=True)
        done=w.drive(ex,admission)
        proof=done['numeric_result']['pending_targets'][0]['measure_conflict_proof']
        self.assertEqual(proof['held_quote_id'],proof['answer_quote_id'])
        self.assertEqual(done['research_status'],'numeric_waiting_evidence')
        self.assertEqual(len(done['numeric_result']['completed_targets']),2)

    def test_forged_original_or_nonindependent_proof_is_not_downgraded_to_pending_conflict(self):
        h,w,f,a,admission,ex,p,v=self.fixture()
        self.seed_collision(h,f,a,admission,ex,p)
        with patch.object(a.registry,'read',side_effect=RuntimeError('Original could not be verified')):
            with self.assertRaisesRegex(RuntimeError,'Original could not be verified'):w.drive(ex,admission)
        self.assertEqual(f.store.connection.execute('SELECT COUNT(*) FROM mission_document_numeric_results').fetchone()[0],0)
        self.assertEqual((p.calls,v.calls),(1,1))

    def test_conflict_receipt_requires_replayed_exact_source_and_field_differences(self):
        h,w,f,a,admission,ex,p,v=self.fixture()
        self.seed_collision(h,f,a,admission,ex,p)
        receipt=w.drive(ex,admission)['numeric_result'];works=h.works(ex,receipt)
        formal=ex.scheduler.formal_result(works[3]['id'])
        h.corruptible_copy(f.store.connection,'mission_document_numeric_results')
        for field in ('figure_hash','document_version_hash','draft_proof_hash','held_read_proof_hash'):
            changed=copy.deepcopy(receipt);proof=changed['pending_targets'][0]['measure_conflict_proof'];proof[field]='f'*64
            proof['content_hash']=content_hash({k:x for k,x in proof.items() if k!='content_hash'})
            changed['content_hash']=content_hash({k:x for k,x in changed.items() if k!='content_hash'})
            f.store.connection.execute('UPDATE mission_document_numeric_results SET record_json=?,content_hash=? WHERE admission_ref=?',
                (canonical_json(changed),changed['content_hash'],admission['id']));f.store.connection.commit()
            offered=copy.deepcopy(formal);offered['result_envelope']['outputs']=changed;offered['result_envelope_hash']=content_hash(offered['result_envelope'])
            with patch.object(ex,'_stage_owned'),self.assertRaises(DirectedNumericError):read_numeric_result(ex,admission,works,offered)
        f.store.connection.execute('UPDATE mission_document_numeric_results SET record_json=?,content_hash=? WHERE admission_ref=?',
            (canonical_json(receipt),receipt['content_hash'],admission['id']));f.store.connection.commit()
        self.assertEqual(read_numeric_result(ex,admission,works,formal)['numeric_result'],receipt)
