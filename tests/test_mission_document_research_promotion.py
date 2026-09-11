import unittest
import json
from pathlib import Path
from unittest.mock import patch

import dalton_core.mission_document_research_promotion as promotion_module
from dalton_core.mission_document_research_executor import effective_mission_document_work_orders
from dalton_core.mission_document_research_promotion import (
    authorize_document_candidate,
    persist_document_promotion,
    promote_document_candidate,
)
from dalton_core.research_auto_commit import ResearchAutoCommitRejected
from tests import test_mission_document_research as fixtures


class DocumentPromotionTests(unittest.TestCase):
    _fixture = fixtures.MissionDocumentResearchTests._fixture
    _executor = fixtures.MissionDocumentResearchTests._executor
    _record_plan = fixtures.MissionDocumentResearchTests._record_plan

    @staticmethod
    def _drive(executor, admission):
        for _ in range(30):
            outcome = executor.run_once(admission['id'])
            if outcome['status'] == 'complete':
                return outcome
        raise AssertionError('directed-document execution did not complete')

    def _completed(self, *, enabled=True, adapter_type=None, recovery=False):
        fixture, authority, args, _, _ = self._fixture(auto_commit=enabled)
        if recovery:
            fixtures.MissionDocumentResearchTests._enable_recovery(fixture)
        admission = authority.admit_from_plan(**args)
        executor, draft, verifier = self._executor(fixture, authority)
        if adapter_type is not None:
            executor, draft, verifier = self._executor(
                fixture, authority, draft_adapter=adapter_type(draft.candidate_wire))
        outcome = self._drive(executor, admission)
        admission = authority.resolve_for_execution(admission['id'])
        works = effective_mission_document_work_orders(
            authority, executor.scheduler, admission['id'],
            draft_worker=executor.draft_worker, verifier_worker=executor.verifier_worker)
        records = executor.scheduler.formal_result(works[3]['id'])['result_envelope']['outputs']
        return fixture, executor, admission, works, records, outcome, draft, verifier

    def test_exact_wiki_original_promotes_once_and_replays_without_model_calls(self):
        fixture, executor, admission, works, records, outcome, draft, verifier = self._completed()
        promoted = outcome
        self.assertEqual(promoted['research_status'], 'canonical_claim_promoted')
        self.assertEqual(promoted, executor.run_once(admission['id']))
        self.assertEqual((draft.calls, verifier.calls), (1, 1))
        self.assertEqual(fixture.store.connection.execute(
            'SELECT count(*) FROM mission_document_research_promotions').fetchone()[0], 1)
        self.assertIsNotNone(fixture.store.get_claim(promoted['claim_version_ref']))

    def test_estimated_accounting_is_bound_with_full_retained_reservation(self):
        fixture, executor, admission, works, records, outcome, _, _ = self._completed(
            adapter_type=fixtures.EstimatedCostAdapter)
        self.assertEqual(outcome['research_status'], 'canonical_claim_promoted')
        saved = json.loads(fixture.store.connection.execute(
            'SELECT record_json FROM mission_document_research_promotions').fetchone()[0])
        accounting = saved['execution_proof']['accounting_proofs'][0]
        self.assertEqual(accounting['cost_status'], 'estimated')
        self.assertIsNone(accounting['budget_settlement_ref'])
        admission_budget = executor.draft_worker.budget_store.admission(
            work_order_ref=works[1]['id'], attempt_number=1, phase='assessment')
        self.assertEqual(accounting['reserved_micros'], admission_budget['admission']['reserved_micros'])
        self.assertGreater(accounting['reserved_micros'], 0)
        self.assertIsNone(admission_budget['settlement'])

    def test_recovered_success_promotes_effective_work_without_rewriting_failure(self):
        fixture, executor, admission, works, records, outcome, draft, _ = self._completed(
            adapter_type=fixtures.CapacityOnceAdapter, recovery=True)
        self.assertEqual(outcome['research_status'], 'canonical_claim_promoted')
        saved = json.loads(fixture.store.connection.execute(
            'SELECT record_json FROM mission_document_research_promotions').fetchone()[0])
        self.assertNotEqual(works[1]['id'], draft.failed_work_id)
        self.assertEqual(saved['execution_proof']['stages'][1]['work_ref'], works[1]['id'])
        self.assertEqual(saved['execution_proof']['accounting_proofs'][0]['work_order_ref'], works[1]['id'])
        self.assertEqual(executor.scheduler.status(draft.failed_work_id)['state'], 'failed')

    def test_disabled_policy_keeps_candidate_staged(self):
        _, executor, admission, works, records, outcome, _, _ = self._completed(enabled=False)
        self.assertEqual(outcome['research_status'], 'candidate_staged')
        self.assertEqual(executor.run_once(admission['id']), outcome)

    def test_failed_proof_insert_rolls_back_entire_ledger_transaction(self):
        fixture, authority, args, _, _ = self._fixture(auto_commit=True)
        admission = authority.admit_from_plan(**args)
        executor, draft, verifier = self._executor(fixture, authority)
        original = fixture.store.commit_policy_candidate
        def fail_after_proof(**kwargs):
            return original(**kwargs, fault_at='after_document_promotion')
        before = fixture.store.connection.execute('SELECT count(*) FROM claim_versions').fetchone()[0]
        with patch.object(fixture.store, 'commit_policy_candidate', side_effect=fail_after_proof):
            with self.assertRaisesRegex(RuntimeError, 'after document promotion'):
                self._drive(executor, admission)
        self.assertEqual(fixture.store.connection.execute('SELECT count(*) FROM claim_versions').fetchone()[0], before)
        self.assertEqual(fixture.store.connection.execute('SELECT count(*) FROM mission_document_research_promotions').fetchone()[0], 0)
        promoted = self._drive(executor, admission)
        self.assertEqual(promoted['research_status'], 'canonical_claim_promoted')
        self.assertEqual((draft.calls, verifier.calls), (1, 1))

    def test_commit_success_followed_by_lost_response_replays_existing_proof(self):
        fixture, authority, args, _, _ = self._fixture(auto_commit=True)
        admission = authority.admit_from_plan(**args)
        executor, draft, verifier = self._executor(fixture, authority)
        store = executor.authority.store
        original = store.commit_policy_candidate
        def lost_response(**kwargs):
            original(**kwargs)
            raise RuntimeError('lost response after commit')
        with patch.object(store, 'commit_policy_candidate', side_effect=lost_response):
            with self.assertRaisesRegex(RuntimeError, 'lost response'):
                self._drive(executor, admission)
        promoted = self._drive(executor, admission)
        self.assertEqual(promoted['research_status'], 'canonical_claim_promoted')
        self.assertEqual((draft.calls, verifier.calls), (1, 1))

    def test_restart_after_candidate_outcome_promotes_without_model_or_budget_replay(self):
        fixture, authority, args, _, _ = self._fixture(auto_commit=True)
        admission = authority.admit_from_plan(**args)
        fired = []

        def crash_after_outcome(seam):
            if seam == 'after_candidate_outcome':
                fired.append(seam)
                raise RuntimeError('crash after candidate outcome')

        executor, draft, verifier = self._executor(
            fixture, authority, fault_injector=crash_after_outcome)
        with self.assertRaisesRegex(RuntimeError, 'crash after candidate outcome'):
            self._drive(executor, admission)
        self.assertEqual(fired, ['after_candidate_outcome'])
        self.assertEqual(fixture.store.connection.execute(
            'SELECT count(*) FROM mission_document_research_outcomes').fetchone()[0], 1)
        promotion_table = fixture.store.connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='mission_document_research_promotions'").fetchone()
        if promotion_table is not None:
            self.assertEqual(fixture.store.connection.execute(
                'SELECT count(*) FROM mission_document_research_promotions').fetchone()[0], 0)
        budget_count = fixture.budget.connection.execute(
            'SELECT count(*) FROM thesis_impact_day_admissions').fetchone()[0]

        restarted, _, _ = self._executor(
            fixture, authority, draft_adapter=draft, verifier_adapter=verifier)
        promoted = restarted.run_once(admission['id'])
        self.assertEqual(promoted['research_status'], 'canonical_claim_promoted')
        self.assertEqual(promoted, restarted.run_once(admission['id']))
        self.assertEqual((draft.calls, verifier.calls), (1, 1))
        self.assertEqual(fixture.budget.connection.execute(
            'SELECT count(*) FROM thesis_impact_day_admissions').fetchone()[0], budget_count)
        self.assertEqual(fixture.store.connection.execute(
            'SELECT count(*) FROM mission_document_research_promotions').fetchone()[0], 1)

    def test_json_candidate_cannot_supply_execution_authority(self):
        fixture, executor, admission, _, records, _, _, _ = self._completed()
        bundle = executor.staging.exact_candidate_bundle(
            evidence_ref=records['candidate_evidence_ref'], claim_ref=records['candidate_claim_ref'],
            idempotency_key=f"mission-document-research-candidate:{admission['id']}")
        with self.assertRaisesRegex(ResearchAutoCommitRejected, 'live executor capability'):
            fixture.store.commit_policy_candidate(**{key: bundle[key] for key in
                ('evidence', 'claim', 'material', 'source_verification')}, idempotency_key='untrusted-json')

    def test_public_persist_hook_rejects_fabricated_ledger_rows(self):
        fixture, executor, admission, _, records, _, _, _ = self._completed()
        prior_promotions = fixture.store.connection.execute(
            'SELECT record_json,content_hash FROM mission_document_research_promotions').fetchall()
        fixture.store.connection.executescript(Path(
            promotion_module.__file__).with_name(
                'mission_document_research_promotion_schema.sql').read_text(encoding='utf-8'))
        bundle = executor.staging.exact_candidate_bundle(
            evidence_ref=records['candidate_evidence_ref'], claim_ref=records['candidate_claim_ref'],
            idempotency_key=f"mission-document-research-candidate:{admission['id']}")
        decision = authorize_document_candidate(
            connection=fixture.store.connection, store=fixture.store, context=executor,
            policy_version=fixture.store.active_policy(), evidence=bundle['evidence'],
            claim=bundle['claim'], material=bundle['material'],
            source_verification=bundle['source_verification'])
        fake = 'f' * 64
        with self.assertRaisesRegex(
            ResearchAutoCommitRejected,
            'material differs from exact staging',
        ):
            with fixture.store._transaction() as cursor:
                persist_document_promotion(
                    cursor,
                    executor,
                    decision,
                    {'id': 'evidence-version:forged', 'content_hash': fake},
                    {'id': 'claim-version:forged', 'content_hash': fake},
                    {'normalized_payload': {
                        'mission_document_admission': {'ref': admission['id']},
                    }},
                )
        candidate_evidence = bundle['evidence']
        forged_evidence = {
            **{key: candidate_evidence[key] for key in (
                'source_type', 'source_ref', 'source_envelope_ref', 'source_envelope_hash',
                'retrieved_at', 'valid_until', 'artifact_refs', 'source_lineage',
                'independence_group', 'source_verification_ref', 'source_verification_hash',
            )},
            'id': 'evidence-version:forged', 'content_hash': fake,
            'candidate_origin_ref': candidate_evidence['id'],
            'candidate_origin_hash': candidate_evidence['content_hash'],
            'review_decision_ref': decision['id'],
            'review_decision_hash': decision['content_hash'],
        }
        candidate_claim = bundle['claim']
        forged_claim = {
            **{key: candidate_claim[key] for key in (
                'subject_ref', 'metric_or_aspect', 'period', 'basis',
                'normalized_statement', 'claim_kind', 'value', 'unit', 'currency', 'scale',
            )},
            'id': 'claim-version:forged', 'content_hash': fake,
            'candidate_origin_ref': candidate_claim['id'],
            'candidate_origin_hash': candidate_claim['content_hash'],
            'semantic_review_ref': decision['id'],
            'semantic_review_hash': decision['content_hash'],
            'producer_execution_refs': [bundle['material']['normalized_payload'][
                'draft_proof']['model_invocation_ref']],
        }
        with self.assertRaisesRegex(
            ResearchAutoCommitRejected,
            'Evidence authority is unavailable',
        ):
            with fixture.store._transaction() as cursor:
                persist_document_promotion(
                    cursor, executor, decision, forged_evidence, forged_claim,
                    bundle['material'])
        self.assertEqual(fixture.store.connection.execute(
            'SELECT record_json,content_hash FROM mission_document_research_promotions').fetchall(), prior_promotions)
        self.assertEqual(bundle['material']['normalized_payload'][
            'mission_document_admission']['ref'], admission['id'])


if __name__ == '__main__':
    unittest.main()
