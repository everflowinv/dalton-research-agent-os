"""Late paid replies keep their original route/fee identity on a later lease."""
import copy
import unittest
from datetime import timedelta
from unittest.mock import patch
from dalton_core.mission_document_research_executor import _effective_stage, _paid_contract_failure_proof
from dalton_core.document_paid_replay import paid_replay_attempt
from dalton_core.store import content_hash
from tests import test_directed_numeric_workflow as fixtures


class DocumentPaidReplayTests(unittest.TestCase):
    def fixture(self):
        h = fixtures.NumericWorkflowTests('runTest')
        self.addCleanup(h.doCleanups)
        f, a, admission, e, p, v = h.fixture()
        p.candidate_wire['targets'][0]['figure']['period'] = 'FY2024'
        original = p.execute
        saved = []
        def late(work, route, profile):
            result = original(work, route, profile)
            saved.append(result)
            f.harness.clock.value += timedelta(seconds=2)
            return result
        p.execute = late
        p.replay = lambda *args: saved[-1]
        e.draft_worker.lease_seconds = 1
        for _ in range(12):
            result = e.run_once(admission['id'])
            if result.get('reason') == 'automatic_bounded_contract_retry': break
            if result.get('status') == 'admitted' and result.get('stage') == 'qualitative_model_draft':
                # Historical Scheduler rows retain four leases despite the
                # Work's single-send cap (the exact live EPAM grandfathered shape).
                # Simulate that old row only in this disposable fixture.
                for row in f.store.connection.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='scheduler_work_orders'").fetchall():
                    f.store.connection.execute('DROP TRIGGER "' + row[0] + '"')
                f.store.connection.execute('UPDATE scheduler_work_orders SET max_attempts=4 WHERE work_order_id=?',
                                          (result['work_order_ref'],))
            f.harness.clock.value += timedelta(seconds=30)
            e.scheduler.sweep_expired()
        self.assertEqual(result.get('reason'), 'automatic_bounded_contract_retry', str({k:result.get(k) for k in ('status','reason','stage')}))
        recovery, links = _effective_stage(a, e.scheduler, admission, 1, worker=e.draft_worker)
        failed = e.scheduler.work_order_authority(links[-1]['failed_work_order_ref'])['work_order']
        formal = e.scheduler.formal_result(failed['id'])
        return f, a, admission, e, p, failed, formal, recovery, links

    def test_paid_expired_lease_replay_rebuilds_existing_recovery_without_new_call(self):
        f, a, admission, e, p, failed, formal, recovery, links = self.fixture()
        route = e.draft_worker.router.get_decision(formal['result_envelope']['metadata']['route_decision_ref'])
        self.assertGreater(formal['attempt_number'], route['attempt_number'])
        before = f.store.connection.total_changes
        proof = _paid_contract_failure_proof(a, failed, formal, 1, e.draft_worker)
        self.assertEqual(proof['classification'], 'proved_paid_output_contract_failure')
        self.assertGreater(proof['actual_micros'], 0)
        self.assertEqual(proof['budget_settlement_hash'], links[-1]['failure_proof']['paid_contract_proof']['budget_settlement_hash'])
        checked, same_links = _effective_stage(a, e.scheduler, admission, 1, worker=e.draft_worker)
        self.assertEqual((checked, same_links), (recovery, links))
        self.assertEqual(e.scheduler.work_order_authority(recovery['id'])['work_order'], recovery)
        self.assertEqual(f.store.connection.total_changes, before)
        self.assertEqual(p.calls, 1)

    def test_pending_settlement_cannot_prove_paid_or_transfer_route_attempt(self):
        f, a, admission, e, p, failed, formal, recovery, links = self.fixture()
        budget = e.draft_worker.budget_store
        original = budget.admission
        def pending(**kwargs):
            result = original(**kwargs)
            if result is not None:
                result = {**result, 'settlement': None}
            return result
        with patch.object(budget, 'admission', side_effect=pending):
            self.assertIsNone(_paid_contract_failure_proof(a, failed, formal, 1, e.draft_worker))
            with self.assertRaisesRegex(RuntimeError, 'failed route authority drifted'):
                _effective_stage(a, e.scheduler, admission, 1, worker=e.draft_worker)
        self.assertEqual(p.calls, 1)

    def test_forged_formal_or_a_newer_route_cannot_reuse_old_attempt(self):
        f, a, admission, e, p, failed, formal, recovery, links = self.fixture()
        worker = e.draft_worker
        damaged = copy.deepcopy(formal); damaged['content_hash'] = '0'*64
        self.assertIsNone(_paid_contract_failure_proof(a, failed, damaged, 1, worker))
        actual = worker.router.list_decisions(work_order_id=failed['id'])
        with patch.object(worker.router, 'list_decisions', return_value=[*actual, {'id': 'route:another'}]):
            self.assertIsNone(_paid_contract_failure_proof(a, failed, formal, 1, worker))
        self.assertEqual(p.calls, 1)
