"""Exact local source replay for duplicate figures from different windows."""
import copy
import hashlib
import json
import unittest
from unittest.mock import patch
from datetime import timedelta

from tests import test_directed_numeric_workflow as workflow
from dalton_core.store import canonical_json, content_hash
from dalton_core.directed_numeric_research import CONTRACT_REF, DirectedNumericError
from dalton_core.directed_numeric_executor import source_binding, _execution_proof, read_numeric_result
from dalton_core.directed_numeric_figure_support import SUPPORT_CONTRACT, figure_support, verify_support
from dalton_core.document_research import READ_REQUEST_SCHEMA_VERSION, READ_OPERATION
from dalton_core.claim_index_figures import MissionFigureAuthorityResolver, promote_figure, _FIGURE_COLUMNS


class NumericFigureSupportTests(unittest.TestCase):
    def fixture(self, **kwargs):
        h=workflow.NumericWorkflowTests(methodName='runTest')
        self.addCleanup(h.doCleanups)
        return h, *h.fixture(extra_text=' Other commitments were $12.3 billion at FY2025.', **kwargs)

    def seed(self, f, a, admission, executor, producer, *, indexes=(0,), canonical=False, same_quote=False):
        proof=a.registry.search(admission['request'])
        request={k:proof['request'][k] for k in ('purpose','research_question','registration','policy_ref','policy_hash')}
        request.update(schema_version=READ_REQUEST_SCHEMA_VERSION,operation=READ_OPERATION,
                       source_start=1,source_end=len(workflow.TEXT))
        old=a.registry.read(request)
        quote=f"quote:1:{len(workflow.TEXT)}:{old['text_sha256'][:16]}"
        if same_quote:
            from dalton_core.directed_numeric_output import quotes
            quote, text=next(iter(quotes(proof).items()))
        else:text=old['text']
        source=source_binding(f.store.connection,admission['request']['registration'],admission['company_ref'])
        for index in indexes:
            candidate={**producer.candidate_wire['targets'][index]['figure'],'quote_id':quote,'citation_text':text}
            a._missions.record_document_figures(company_ref=admission['company_ref'],review_ref=source['review_id'],
                document_ref=source['document_ref'],source_manifest_hash=source['source_manifest_hash'],source_grade=source['source_grade'],
                figures=[candidate],observed_by=admission['actor_ref'],numeric_contract_ref=CONTRACT_REF)
        figures=a._missions.document_figures(company_ref=admission['company_ref'],limit=2000)
        if canonical:
            for figure in figures:
                bundle=promote_figure(f.store,executor.staging,figure=figure,actor_ref=admission['actor_ref'],
                    idempotency_key='old-synthetic-figure:'+figure['figure_id'])
                f.store.commit_policy_candidate(evidence=bundle['evidence'],claim=bundle['claim'],material=bundle['material'],
                    source_verification=bundle['source_verification'],numeric_spec=None,numeric_verification=bundle['numeric_verification'],
                    idempotency_key='old-synthetic-canonical:'+figure['figure_id'])
        return figures

    def works(self, executor, receipt):
        result=[None]*4
        current=receipt['work_order_ref']
        for index in range(3,-1,-1):
            result[index]=executor.scheduler.work_order_authority(current)['work_order']
            if index:current=result[index]['metadata']['upstream_work_order_ref']
        return result

    def support_args(self, executor, admission, receipt):
        works=self.works(executor,receipt)
        proof,draft,verifier,_=_execution_proof(executor,admission,works)
        item=receipt['figure_supports'][0]
        target=next(t for t in admission['numeric_authority']['targets'] if t['target_ref']==item['target_ref'])
        figure=MissionFigureAuthorityResolver(executor.connection).figure(item['figure_ref'])
        return dict(connection=executor.connection,registry=executor.registry,admission=admission,
                    target=target,figure=figure,search_proof=proof,draft=draft,verifier=verifier)

    def corruptible_copy(self, connection, table):
        # Adversarial corruption happens only in the synthetic fixture. The
        # production append-only triggers remain untouched by the patch.
        triggers=connection.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name=?",(table,)).fetchall()
        for row in triggers:connection.execute('DROP TRIGGER '+row[0])
        connection.commit()

    def rows(self, connection, table):
        return [tuple(r) for r in connection.execute('SELECT * FROM '+table+' ORDER BY rowid')]

    def test_different_windows_reuse_one_figure_preserve_canonical_bytes_and_replay_without_calls(self):
        h,f,a,admission,ex,p,v=self.fixture()
        old=self.seed(f,a,admission,ex,p,canonical=True)[0]
        old_claims=self.rows(f.store.connection,'claim_versions')
        old_evidence=self.rows(f.store.connection,'evidence_versions')
        done=h.drive(ex,admission);receipt=done['numeric_result']
        self.assertEqual(done['research_status'],'numeric_targets_completed')
        self.assertEqual(receipt['figure_support_contract'],SUPPORT_CONTRACT)
        support=receipt['figure_supports'][0]
        self.assertEqual(support['figure_ref'],old['figure_id'])
        self.assertEqual(support['figure_hash'],old['content_hash'])
        self.assertNotEqual(support['held_quote_id'],support['answer_quote_id'])
        self.assertEqual(MissionFigureAuthorityResolver(f.store.connection).figure(old['figure_id']),old)
        self.assertTrue(all(r in self.rows(f.store.connection,'claim_versions') for r in old_claims))
        self.assertTrue(all(r in self.rows(f.store.connection,'evidence_versions') for r in old_evidence))
        self.assertEqual(len(a._missions.document_figures(company_ref=admission['company_ref'],limit=2000)),3)
        self.assertEqual(verify_support(support,**self.support_args(ex,admission,receipt)),support)
        before=[self.rows(f.store.connection,t) for t in ('mission_document_numeric_results','coverage_mission_document_figures')]
        self.assertEqual(ex.run_once(admission['id']),done)
        self.assertEqual((p.calls,v.calls),(1,1))
        self.assertEqual(before,[self.rows(f.store.connection,t) for t in ('mission_document_numeric_results','coverage_mission_document_figures')])

    def test_partial_keeps_missing_target_and_supports_only_the_accepted_fact(self):
        h,f,a,admission,ex,p,v=self.fixture(missing=True)
        self.seed(f,a,admission,ex,p)
        done=h.drive(ex,admission)
        self.assertEqual(done['research_status'],'numeric_waiting_evidence')
        self.assertEqual(len(done['numeric_result']['completed_targets']),2)
        self.assertEqual(len(done['numeric_result']['figure_supports']),1)
        self.assertEqual(done['numeric_result']['pending_targets'][0]['target_ref'],'target:leases')
        self.assertEqual(done,ex.run_once(admission['id']))
        self.assertEqual((p.calls,v.calls),(1,1))

    def test_mapping_definition_figure_quotes_and_formal_proof_hashes_cannot_be_substituted(self):
        h,f,a,admission,ex,p,v=self.fixture()
        self.seed(f,a,admission,ex,p)
        done=h.drive(ex,admission);receipt=done['numeric_result'];args=self.support_args(ex,admission,receipt)
        for field in ('metric_definition_hash','target_hash','figure_hash','document_version_hash','held_quote_hash',
                      'answer_quote_hash','source_proof_hash','held_read_proof_hash','draft_proof_hash','verifier_proof_hash'):
            with self.subTest(field=field):
                changed=copy.deepcopy(receipt['figure_supports'][0]);changed[field]='f'*64
                changed['content_hash']=content_hash({k:x for k,x in changed.items() if k!='content_hash'})
                with self.assertRaisesRegex(DirectedNumericError,'mapping does not replay'):
                    verify_support(changed,**args)
        bad={**args,'search_proof':copy.deepcopy(args['search_proof'])};bad['search_proof']['matches'][0]['excerpt']+=' forged'
        with self.assertRaises(Exception):figure_support(**bad)
        bad={**args,'verifier':copy.deepcopy(args['verifier'])};bad['verifier']['model_family']=args['draft']['model_family']
        with self.assertRaisesRegex(DirectedNumericError,'independent'):
            figure_support(**bad)

    def test_completed_receipt_replay_uses_the_same_support_check_not_a_marker_bypass(self):
        h,f,a,admission,ex,p,v=self.fixture()
        self.seed(f,a,admission,ex,p)
        receipt=h.drive(ex,admission)['numeric_result'];works=self.works(ex,receipt)
        formal=ex.scheduler.formal_result(works[3]['id'])
        self.corruptible_copy(f.store.connection,'mission_document_numeric_results')
        for change in ('bad-proof','remove-marker'):
            changed=copy.deepcopy(receipt)
            if change=='bad-proof':changed['figure_supports'][0]['held_quote_hash']='f'*64
            else:changed.pop('figure_support_contract');changed.pop('figure_supports')
            changed['content_hash']=content_hash({k:x for k,x in changed.items() if k!='content_hash'})
            f.store.connection.execute('UPDATE mission_document_numeric_results SET record_json=?,content_hash=? WHERE admission_ref=?',
                (canonical_json(changed),changed['content_hash'],admission['id']))
            f.store.connection.commit()
            offered=copy.deepcopy(formal);offered['result_envelope']['outputs']=changed
            offered['result_envelope_hash']=content_hash(offered['result_envelope'])
            # The normal ownership gate already rejects a forged formal hash.
            with self.assertRaises(Exception):read_numeric_result(ex,admission,works,offered)
            # Isolate the subsequent receipt/support check as a second gate.
            with patch.object(ex,'_stage_owned'), self.assertRaises(DirectedNumericError):
                read_numeric_result(ex,admission,works,offered)
        f.store.connection.execute('UPDATE mission_document_numeric_results SET record_json=?,content_hash=? WHERE admission_ref=?',
            (canonical_json(receipt),receipt['content_hash'],admission['id']));f.store.connection.commit()
        self.assertEqual(read_numeric_result(ex,admission,works,formal)['numeric_result'],receipt)
        self.assertEqual((p.calls,v.calls),(1,1))

    def test_same_amount_different_measure_or_fact_identity_still_refuses(self):
        h,f,a,admission,ex,p,v=self.fixture()
        old=self.seed(f,a,admission,ex,p)[0]
        done=h.drive(ex,admission);args=self.support_args(ex,admission,done['numeric_result'])
        self.corruptible_copy(f.store.connection,'coverage_mission_document_figures')
        for field,value in (('value','2.5'),('as_reported_label','Uncommenced leases'),
                            ('metric_ref','metric:other-measure'),('basis','reported'),
                            ('company_ref','company:other'),('document_ref','document:other'),
                            ('source_manifest_hash','f'*64),('period','FY2024'),('unit','percent'),('currency','EUR')):
            with self.subTest(field=field):
                for key in _FIGURE_COLUMNS:f.store.connection.execute('UPDATE coverage_mission_document_figures SET '+key+'=? WHERE figure_id=?',(old[key],old['figure_id']))
                changed={**old,field:value};changed['content_hash']=content_hash({k:changed[k] for k in _FIGURE_COLUMNS})
                f.store.connection.execute('UPDATE coverage_mission_document_figures SET '+field+'=?,content_hash=? WHERE figure_id=?',
                    (value,changed['content_hash'],old['figure_id']));f.store.connection.commit()
                with self.assertRaises(Exception):figure_support(**{**args,'figure':changed})
        for field in ('value','as_reported_label','metric_ref','basis','company_ref','document_ref','source_manifest_hash','period','unit','currency'):
            f.store.connection.execute('UPDATE coverage_mission_document_figures SET '+field+'=? WHERE figure_id=?',(old[field],old['figure_id']))
        f.store.connection.execute('UPDATE coverage_mission_document_figures SET content_hash=? WHERE figure_id=?',(old['content_hash'],old['figure_id']));f.store.connection.commit()

    def test_retraction_and_forged_old_original_window_do_not_reinstate_or_reuse(self):
        h,f,a,admission,ex,p,v=self.fixture()
        old=self.seed(f,a,admission,ex,p)[0]
        done=h.drive(ex,admission);args=self.support_args(ex,admission,done['numeric_result'])
        self.corruptible_copy(f.store.connection,'coverage_mission_document_figures')
        # A self-consistent hash plus true-looking digits does not bind a quote to this original range.
        changed={**old,'citation_text':old['citation_text']+' extra bytes'}
        changed['quote_id']='quote:1:'+str(len(workflow.TEXT))+':'+hashlib.sha256(changed['citation_text'].encode()).hexdigest()[:16]
        changed['content_hash']=content_hash({k:changed[k] for k in _FIGURE_COLUMNS})
        f.store.connection.execute('UPDATE coverage_mission_document_figures SET quote_id=?,citation_text=?,content_hash=? WHERE figure_id=?',
            (changed['quote_id'],changed['citation_text'],changed['content_hash'],old['figure_id']));f.store.connection.commit()
        with self.assertRaisesRegex(DirectedNumericError,'registered original'):
            figure_support(**{**args,'figure':changed})
        f.store.connection.execute('UPDATE coverage_mission_document_figures SET quote_id=?,citation_text=?,content_hash=? WHERE figure_id=?',
            (old['quote_id'],old['citation_text'],old['content_hash'],old['figure_id']));f.store.connection.commit()
        a._missions.retract_document_figure(old['figure_id'],retracted_by=admission['actor_ref'],reason='Synthetic bad prior fact')
        with self.assertRaises(Exception):figure_support(**args)

    def test_same_quote_canonical_duplicate_reuses_staging_and_records_true_old_commit_key(self):
        h,f,a,admission,ex,p,v=self.fixture()
        old=self.seed(f,a,admission,ex,p,canonical=True,same_quote=True)[0]
        done=h.drive(ex,admission)
        support=done['numeric_result']['figure_supports'][0]
        self.assertEqual(support['held_quote_id'],support['answer_quote_id'])
        binding=next(c for c in done['numeric_result']['canonical_claims'] if c['target_ref']==support['target_ref'])
        self.assertEqual(binding['canonical_commit_support']['idempotency_key'],'old-synthetic-canonical:'+old['figure_id'])
        self.assertEqual(done,ex.run_once(admission['id']))
        self.assertEqual((p.calls,v.calls),(1,1))

    def test_failed_local_quote_completion_can_resume_paid_proofs_without_another_model_attempt(self):
        h,f,a,admission,ex,p,v=self.fixture()
        self.seed(f,a,admission,ex,p)
        with patch('dalton_core.directed_numeric_figure_support.figure_support',side_effect=DirectedNumericError('Synthetic old strict quote refusal')):
            with self.assertRaisesRegex(DirectedNumericError,'old strict quote refusal'):h.drive(ex,admission)
        self.assertEqual((p.calls,v.calls),(1,1))
        work_rows=self.rows(ex.scheduler.connection,'scheduler_work_orders')
        budget_rows=self.rows(ex.draft_worker.budget_store.connection,'thesis_impact_day_settlements')
        f.harness.clock.value+=timedelta(seconds=601)
        done=h.drive(ex,admission)
        self.assertEqual(done['research_status'],'numeric_targets_completed')
        self.assertEqual(work_rows,self.rows(ex.scheduler.connection,'scheduler_work_orders'))
        self.assertEqual(budget_rows,self.rows(ex.draft_worker.budget_store.connection,'thesis_impact_day_settlements'))
        self.assertEqual((p.calls,v.calls),(1,1))

    def test_frozen_canonical_origin_survives_a_later_compatible_legacy_commit_row(self):
        from dalton_core.directed_numeric_figure_support import canonical_commit_support
        from dalton_core.claim_index_figures import build_figure_candidate
        h,f,a,admission,ex,p,v=self.fixture()
        self.seed(f,a,admission,ex,p,canonical=True)
        done=h.drive(ex,admission);receipt=done['numeric_result']
        support=receipt['figure_supports'][0]
        committed=next(c for c in receipt['canonical_claims'] if c['target_ref']==support['target_ref'])
        original_key=committed['canonical_commit_support']['idempotency_key']
        connection=f.store.connection
        schema=connection.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='reviewed_candidate_commits'").fetchone()[0]
        # Current schema's UNIQUE refs forbid this multiplicity. Simulate only
        # a legacy compatible shape without changing product schema or rows.
        connection.execute('ALTER TABLE reviewed_candidate_commits RENAME TO single_origin_fixture')
        connection.execute(schema.replace(' NOT NULL UNIQUE',' NOT NULL'))
        connection.execute('INSERT INTO reviewed_candidate_commits SELECT * FROM single_origin_fixture')
        original=dict(connection.execute('SELECT * FROM reviewed_candidate_commits WHERE idempotency_key=?',(original_key,)).fetchone())
        later={**original,'idempotency_key':'a-later-compatible-key'}
        saved=json.loads(later['result_json']);saved['idempotency_key']=later['idempotency_key']
        later['result_json']=canonical_json(saved)
        connection.execute('INSERT INTO reviewed_candidate_commits('+','.join(later)+') VALUES('+','.join('?' for _ in later)+')',tuple(later.values()))
        connection.commit()
        self.assertEqual(done,ex.run_once(admission['id']))
        figure=MissionFigureAuthorityResolver(connection).figure(support['figure_ref'])
        bundle=build_figure_candidate(connection,figure=figure,actor_ref=admission['actor_ref'])
        rebuilt=canonical_commit_support(f.store,bundle=bundle,committed=committed,idempotency_key=original_key)
        self.assertEqual(rebuilt,committed['canonical_commit_support'])
        self.assertEqual(canonical_commit_support(f.store,bundle=bundle,committed=committed)['idempotency_key'],original_key)
        with self.assertRaisesRegex(DirectedNumericError,'admission is absent'):
            canonical_commit_support(f.store,bundle=bundle,committed=committed,idempotency_key='nonexistent-key')
        self.assertEqual((p.calls,v.calls),(1,1))

    def test_existing_same_window_receipt_has_no_new_contract_and_keeps_old_bytes(self):
        h,f,a,admission,ex,p,v=self.fixture()
        done=h.drive(ex,admission)
        self.assertNotIn('figure_support_contract',done['numeric_result'])
        self.assertNotIn('figure_supports',done['numeric_result'])
        before=canonical_json(done['numeric_result'])
        self.assertEqual(before,canonical_json(ex.run_once(admission['id'])['numeric_result']))
        self.assertEqual((p.calls,v.calls),(1,1))
