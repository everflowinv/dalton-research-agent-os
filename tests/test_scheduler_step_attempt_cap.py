"""Per-step attempt ceilings tighten policy without changing historical Work."""
import unittest
from dalton_core.scheduler import Scheduler, SchedulerValidationError
from dalton_core.store import canonical_json, content_hash
from tests import test_scheduler as scheduler_fixture
from tests import test_directed_numeric_workflow as numeric_fixture


class StepAttemptCapTests(unittest.TestCase):
    def setUp(self):
        self.scheduler = Scheduler(':memory:', max_attempts=3)
        self.addCleanup(self.scheduler.close)

    def work(self, cap=1, *, scope='registered_mission_document_read'):
        work = scheduler_fixture.work_order('work:step-ceiling')
        work['metadata'] = {'permission_scope': scope}
        work['runtime_profile_ref'] = 'runtime-profile:dalton-model-broker:0.1'
        work['budget']['step_max_attempts'] = cap
        return work

    def row(self, work):
        return dict(self.scheduler.connection.execute(
            'SELECT * FROM scheduler_work_orders WHERE work_order_id=?', (work['id'],)).fetchone())

    def test_tighter_cap_applies_to_model_runtime_without_permission_scope(self):
        for scope in ('registered_mission_document_read', 'registered_annual_report_read', None):
            with self.subTest(scope=scope):
                work = self.work(scope=scope)
                work['id'] += ':' + str(scope)
                work['idempotency_key'] = 'enqueue:' + work['id']
                self.scheduler.enqueue(work)
                self.assertEqual(self.row(work)['max_attempts'], 1)
                self.assertEqual(self.row(work)['work_order_hash'], content_hash(work))

    def test_non_model_capability_keeps_existing_local_retry_policy(self):
        work = self.work()
        work['runtime_profile_ref'] = 'runtime:test'
        self.assertTrue(work['requested_capabilities'])  # formatter is not a model.
        self.scheduler.enqueue(work)
        self.assertEqual(self.row(work)['max_attempts'], 3)

    def test_annual_scope_preserves_its_original_attempt_contract(self):
        work = self.work(scope='registered_annual_report_read')
        work['runtime_profile_ref'] = 'runtime:test'
        self.scheduler.enqueue(work)
        self.assertEqual(self.row(work)['max_attempts'], 1)

    def test_larger_work_cap_cannot_raise_trusted_scheduler_cap(self):
        work = self.work(99)
        self.scheduler.enqueue(work)
        self.assertEqual(self.row(work)['max_attempts'], 3)
        self.assertEqual(self.row(work)['work_order_json'], canonical_json(work))

    def test_missing_step_cap_preserves_installed_policy(self):
        work = self.work()
        del work['budget']['step_max_attempts']
        work['budget']['max_attempts'] = 999  # This is not a trusted policy override.
        self.scheduler.enqueue(work)
        self.assertEqual(self.row(work)['max_attempts'], 3)

    def test_invalid_new_caps_are_rejected_without_any_work_or_attempt(self):
        for cap in (True, False, 0, -1, 1.5, '1', None):
            with self.subTest(cap=cap):
                with self.assertRaisesRegex(SchedulerValidationError, 'positive integer'):
                    self.scheduler.enqueue(self.work(cap))
                self.assertEqual(self.scheduler.connection.execute(
                    'SELECT COUNT(*) FROM scheduler_work_orders').fetchone()[0], 0)
                self.assertEqual(self.scheduler.connection.execute(
                    'SELECT COUNT(*) FROM scheduler_attempt_events').fetchone()[0], 0)

    def test_retryable_completion_is_terminal_at_work_ceiling(self):
        work = self.work()
        self.scheduler.enqueue(work)
        lease = self.scheduler.claim('worker:test', work_order_id=work['id'])
        result = scheduler_fixture.result('result:one-retry', work_ref=work['id'], status='retryable')
        completed = self.scheduler.complete(
            work['id'], 1, 'worker:test', lease['lease_token'], result,
            idempotency_key='complete:one-retry')
        self.assertEqual(completed['work_state'], 'failed')
        self.assertIsNone(self.scheduler.claim('worker:test', work_order_id=work['id']))
        self.assertEqual({row['attempt_number'] for row in self.scheduler.attempt_history(work['id'])}, {1})
        self.assertEqual(self.scheduler.connection.execute(
            'SELECT COUNT(*) FROM scheduler_result_envelopes').fetchone()[0], 1)

    def test_legacy_row_is_not_rewritten_or_reinterpreted_on_duplicate(self):
        # Reproduce the old genuine row: a mission step declares one attempt,
        # but its persisted Scheduler ceiling was three. Do not migrate it.
        work = self.work()
        with self.scheduler._transaction() as cur:
            cur.execute('INSERT INTO scheduler_work_orders VALUES(?,?,?,?,?,?)',
                        (work['id'], canonical_json(work), content_hash(work),
                         self.scheduler.policy_version_id, 3, work['created_at']))
            self.scheduler._append_event(cur, work_order_id=work['id'],
                attempt_number=1, state='ready', now=work['created_at'], reason='enqueued')
        before = self.row(work)
        history = self.scheduler.attempt_history(work['id'])
        result = self.scheduler.enqueue(work)
        self.assertEqual(result['status'], 'duplicate')
        self.assertEqual(self.row(work), before)
        self.assertEqual(self.scheduler.attempt_history(work['id']), history)
        self.assertEqual(self.row(work)['max_attempts'], 3)

    def test_existing_legacy_invalid_cap_is_read_without_revalidation(self):
        work = self.work(True)
        with self.scheduler._transaction() as cur:
            cur.execute('INSERT INTO scheduler_work_orders VALUES(?,?,?,?,?,?)',
                        (work['id'], canonical_json(work), content_hash(work),
                         self.scheduler.policy_version_id, 3, work['created_at']))
        before = self.row(work)
        self.assertEqual(self.scheduler.enqueue(work)['status'], 'duplicate')
        self.assertEqual(self.row(work), before)

    def test_real_numeric_paid_contract_failure_has_only_one_scheduler_attempt(self):
        import copy
        fixture = numeric_fixture.NumericWorkflowTests(methodName='runTest')
        fixture.addCleanup = self.addCleanup
        _, _, admission, executor, producer, verifier = fixture.fixture()
        malformed = copy.deepcopy(producer.candidate_wire)
        malformed['targets'][0]['figure']['period'] = 'FY2024'
        producer.candidate_wire = malformed
        for _ in range(12):
            executor.run_once(admission['id'])
        self.assertGreaterEqual(producer.calls, 1)
        self.assertEqual(verifier.calls, 0)
        rows = executor.scheduler.connection.execute(
            'SELECT work_order_id,max_attempts FROM scheduler_work_orders '
            "WHERE json_extract(work_order_json,'$.metadata.mission_document_research_admission_ref')=? "
            "AND json_extract(work_order_json,'$.metadata.stage')='qualitative_model_draft'",
            (admission['id'],)).fetchall()
        self.assertGreaterEqual(len(rows), 1)
        # The distinct bounded recovery Work, if issued, has its own immutable
        # admission. Neither Work may silently buy three Scheduler attempts.
        for row in rows:
            self.assertEqual(row['max_attempts'], 1)
            self.assertEqual({r['attempt_number'] for r in executor.scheduler.attempt_history(row['work_order_id'])}, {1})
        self.assertEqual(producer.calls, len(rows))

    def test_real_numeric_workflow_caps_models_and_preserves_local_recovery_policy(self):
        fixture = numeric_fixture.NumericWorkflowTests(methodName='runTest')
        fixture.addCleanup = self.addCleanup
        _, _, admission, executor, producer, verifier = fixture.fixture()
        result = fixture.drive(executor, admission)
        self.assertEqual(result['research_status'], 'numeric_targets_completed')
        self.assertEqual((producer.calls, verifier.calls), (1, 1))
        rows = executor.scheduler.connection.execute(
            'SELECT work_order_id,max_attempts,work_order_json FROM scheduler_work_orders '
            "WHERE json_extract(work_order_json,'$.metadata.mission_document_research_admission_ref')=?",
            (admission['id'],)).fetchall()
        self.assertEqual(len(rows), 4)
        import json
        for row in rows:
            wire = json.loads(row['work_order_json'])
            self.assertEqual(wire['metadata']['mission_document_research_admission_ref'], admission['id'])
            self.assertNotIn('permission_scope', wire['metadata'])
            self.assertEqual(wire['budget']['step_max_attempts'], 1)
            expected = 1 if wire['runtime_profile_ref'] == 'runtime-profile:dalton-model-broker:0.1' else executor.scheduler.max_attempts
            self.assertEqual(row['max_attempts'], expected)
            self.assertEqual({r['attempt_number'] for r in executor.scheduler.attempt_history(row['work_order_id'])}, {1})
