import copy
import json
import unittest
from unittest.mock import patch
from dalton_core.store import content_hash, canonical_json
from dalton_core.document_execution_failure import read_execution_failure, compact_failures
from dalton_core.document_research_feedback import read_document_research_feedback
from dalton_core.research_planner import project_state_for_prompt, build_prompt
from tests import test_directed_numeric_workflow as workflow
from tests import test_research_planner as planner


class DocumentExecutionFailureTests(unittest.TestCase):
    def fixture(self):
        h = workflow.NumericWorkflowTests('runTest')
        self.addCleanup(h.doCleanups)
        return h.fixture()

    def failed(self):
        f, a, admission, e, p, v = self.fixture()
        p.candidate_wire['targets'][0]['figure']['period'] = 'FY2024'
        for _ in range(12):
            result = e.run_once(admission['id'])
            if result.get('reason') == 'automatic_bounded_contract_retry':
                break
        self.assertEqual(result['reason'], 'automatic_bounded_contract_retry')
        return f, a, admission, e, p, v

    def test_existing_recovery_is_pending_not_answered_or_duplicate_permission(self):
        f, a, admission, e, p, v = self.failed()
        before = f.store.connection.total_changes
        failure = read_execution_failure(f.store.connection, admission['id'])
        self.assertEqual(failure['execution_state'], 'recovery_pending')
        self.assertIn('period expected', failure['error_message'])
        self.assertNotEqual(failure['work_order_ref'], failure['failed_work_order_ref'])
        self.assertEqual(a.equivalent_open_admission(admission)['state'], 'recovery_pending')
        self.assertIsNotNone(failure['recovery_link_hash'])
        feedback = read_document_research_feedback(f.store, f.mission)
        self.assertEqual(feedback['status'], 'available')
        rows = [r for r in feedback['by_company'][admission['company_ref']] if r['outcome'] == 'document_execution_failed']
        self.assertEqual(rows, [failure])
        self.assertEqual(f.store.connection.total_changes, before)
        self.assertEqual((p.calls, v.calls), (1, 0))

    def test_terminal_failure_keeps_exact_cause_and_exhausted_limit(self):
        f, a, admission, e, p, v = self.failed()
        for _ in range(20):
            result = e.run_once(admission['id'])
            if result.get('reason') == 'numeric_technical_maintenance_failed':
                break
        failure = read_execution_failure(f.store.connection, admission['id'])
        self.assertEqual(failure['execution_state'], 'execution_failed')
        self.assertEqual(failure['error_code'], 'MODEL_OUTPUT_CONTRACT_REJECTED')
        self.assertIn('period expected', failure['error_message'])
        self.assertEqual(a.equivalent_open_admission(admission)['state'], 'execution_failed')
        before = p.calls
        for _ in range(3):
            e.run_once(admission['id'])
        self.assertEqual(p.calls, before)
        self.assertEqual(compact_failures([failure])['failures'][0]['formal_result_hash'], failure['formal_result_hash'])

    def test_completed_recovery_removes_failed_context(self):
        f, a, admission, e, p, v = self.failed()
        p.candidate_wire['targets'][0]['figure']['period'] = 'FY2025'
        self.assertEqual(workflow.NumericWorkflowTests.drive(self, e, admission)['research_status'], 'numeric_targets_completed')
        self.assertIsNone(read_execution_failure(f.store.connection, admission['id']))
        self.assertEqual(a.equivalent_open_admission(admission)['state'], 'recently_answered')

    def test_formal_hash_corruption_is_refused_not_promoted_to_failed_proof(self):
        f, a, admission, e, p, v = self.failed()
        for _ in range(20):
            result = e.run_once(admission['id'])
            if result.get('reason') == 'numeric_technical_maintenance_failed': break
        failed = read_execution_failure(f.store.connection, admission['id'])
        for row in f.store.connection.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND tbl_name='scheduler_formal_results'").fetchall():
            f.store.connection.execute('DROP TRIGGER "' + row[0] + '"')
        f.store.connection.execute('UPDATE scheduler_formal_results SET content_hash=? WHERE work_order_id=?',
                                  ('0'*64, failed['work_order_ref']))
        with self.assertRaisesRegex(ValueError, 'formal authority differs'):
            read_execution_failure(f.store.connection, admission['id'])

    def test_compacted_prompt_keeps_failure_authority_and_reason(self):
        f, a, admission, e, p, v = self.failed()
        failure = read_execution_failure(f.store.connection, admission['id'])
        state = planner.state(checklist=[planner.entry(admission['company_ref'], 'SYN')],
            document_research_feedback={'status': 'available', 'by_company': {
                admission['company_ref']: [failure]}, 'content_hash': content_hash(failure)})
        company = next(c for c in state['companies'] if c['company_ref'] == admission['company_ref'])
        self.assertEqual(company['document_execution_failures'], compact_failures([failure]))
        projected = project_state_for_prompt(state, max_input_bytes=64000)
        prompt = build_prompt(projected)
        self.assertIn(failure['work_order_hash'], prompt)
        self.assertIn('not answers or source absence', prompt)
        self.assertLessEqual(len(prompt.encode('utf-8')), 64000)
