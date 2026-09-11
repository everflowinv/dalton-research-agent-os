import unittest
import json
from unittest.mock import patch

from dalton_core.mission_document_research_executor import effective_mission_document_work_orders
from dalton_core.mission_document_research_promotion import promote_document_candidate
from dalton_core.research_auto_commit import ResearchAutoCommitRejected
from tests import test_mission_document_research as fixtures


class DocumentPromotionTests(unittest.TestCase):
    _fixture = fixtures.MissionDocumentResearchTests._fixture
    _executor = fixtures.MissionDocumentResearchTests._executor
    _record_plan = fixtures.MissionDocumentResearchTests._record_plan

    def _completed(self, *, enabled=True, adapter_type=None, recovery=False):
        fixture, authority, args, _, _ = self._fixture(auto_commit=enabled)
        if recovery:
            fixtures.MissionDocumentResearchTests._enable_recovery(fixture)
        admission = authority.admit_from_plan(**args)
        executor, draft, verifier = self._executor(fixture, authority)
        if adapter_type is not None:
            executor, draft, verifier = self._executor(
                fixture, authority, draft_adapter=adapter_type(draft.candidate_wire))
        for _ in range(30):
            outcome = executor.run_once(admission['id'])
            if outcome['status'] == 'complete':
                break
        admission = authority.resolve_for_execution(admission['id'])
        self.assertEqual(outcome['status'], 'complete')
        works = effective_mission_document_work_orders(
            authority, executor.scheduler, admission['id'],
            draft_worker=executor.draft_worker, verifier_worker=executor.verifier_worker)
        records = executor.scheduler.formal_result(works[3]['id'])['result_envelope']['outputs']
        return fixture, executor, admission, works, records, outcome, draft, verifier

    def test_exact_wiki_original_promotes_once_and_replays_without_model_calls(self):
        fixture, executor, admission, works, records, outcome, draft, verifier = self._completed()
        promoted = promote_document_candidate(executor, admission, works, records, outcome)
        self.assertEqual(promoted['research_status'], 'canonical_claim_promoted')
        self.assertEqual(promoted, promote_document_candidate(executor, admission, works, records, outcome))
        self.assertEqual((draft.calls, verifier.calls), (1, 1))
        self.assertEqual(fixture.store.connection.execute(
            'SELECT count(*) FROM mission_document_research_promotions').fetchone()[0], 1)
        self.assertIsNotNone(fixture.store.get_claim(promoted['claim_version_ref']))

    def test_estimated_accounting_is_bound_with_full_retained_reservation(self):
        fixture, executor, admission, works, records, outcome, _, _ = self._completed(
            adapter_type=fixtures.EstimatedCostAdapter)
        promote_document_candidate(executor, admission, works, records, outcome)
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
        promote_document_candidate(executor, admission, works, records, outcome)
        saved = json.loads(fixture.store.connection.execute(
            'SELECT record_json FROM mission_document_research_promotions').fetchone()[0])
        self.assertNotEqual(works[1]['id'], draft.failed_work_id)
        self.assertEqual(saved['execution_proof']['stages'][1]['work_ref'], works[1]['id'])
        self.assertEqual(saved['execution_proof']['accounting_proofs'][0]['work_order_ref'], works[1]['id'])
        self.assertEqual(executor.scheduler.status(draft.failed_work_id)['state'], 'failed')

    def test_disabled_policy_keeps_candidate_staged(self):
        _, executor, admission, works, records, outcome, _, _ = self._completed(enabled=False)
        self.assertEqual(promote_document_candidate(executor, admission, works, records, outcome), outcome)

    def test_failed_proof_insert_rolls_back_entire_ledger_transaction(self):
        fixture, executor, admission, works, records, outcome, draft, verifier = self._completed()
        original = fixture.store.commit_policy_candidate
        def fail_after_proof(**kwargs):
            return original(**kwargs, fault_at='after_document_promotion')
        before = fixture.store.connection.execute('SELECT count(*) FROM claim_versions').fetchone()[0]
        with patch.object(fixture.store, 'commit_policy_candidate', side_effect=fail_after_proof):
            with self.assertRaisesRegex(RuntimeError, 'after document promotion'):
                promote_document_candidate(executor, admission, works, records, outcome)
        self.assertEqual(fixture.store.connection.execute('SELECT count(*) FROM claim_versions').fetchone()[0], before)
        self.assertEqual(fixture.store.connection.execute('SELECT count(*) FROM mission_document_research_promotions').fetchone()[0], 0)
        promoted = promote_document_candidate(executor, admission, works, records, outcome)
        self.assertEqual(promoted['research_status'], 'canonical_claim_promoted')
        self.assertEqual((draft.calls, verifier.calls), (1, 1))

    def test_commit_success_followed_by_lost_response_replays_existing_proof(self):
        _, executor, admission, works, records, outcome, draft, verifier = self._completed()
        store = executor.authority.store
        original = store.commit_policy_candidate
        def lost_response(**kwargs):
            original(**kwargs)
            raise RuntimeError('lost response after commit')
        with patch.object(store, 'commit_policy_candidate', side_effect=lost_response):
            with self.assertRaisesRegex(RuntimeError, 'lost response'):
                promote_document_candidate(executor, admission, works, records, outcome)
        promoted = promote_document_candidate(executor, admission, works, records, outcome)
        self.assertEqual(promoted['research_status'], 'canonical_claim_promoted')
        self.assertEqual((draft.calls, verifier.calls), (1, 1))

    def test_json_candidate_cannot_supply_execution_authority(self):
        fixture, executor, admission, _, records, _, _, _ = self._completed()
        bundle = executor.staging.exact_candidate_bundle(
            evidence_ref=records['candidate_evidence_ref'], claim_ref=records['candidate_claim_ref'],
            idempotency_key=f"mission-document-research-candidate:{admission['id']}")
        with self.assertRaisesRegex(ResearchAutoCommitRejected, 'live executor capability'):
            fixture.store.commit_policy_candidate(**{key: bundle[key] for key in
                ('evidence', 'claim', 'material', 'source_verification')}, idempotency_key='untrusted-json')


if __name__ == '__main__':
    unittest.main()
