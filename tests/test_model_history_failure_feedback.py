import copy
import json
import unittest
from unittest.mock import patch

from dalton_core import company_model_spec as specs
from dalton_core.company_financial_statement_structure import (
    FinancialStatementStructureError, materialize_financial_statement_structure,
)
from dalton_core.coverage_mission import company_model_spec_id
from dalton_core.model_history_failure_feedback import describe_failure
from dalton_core.store import content_hash
from tests import test_model_spec_signed_expense_repair as fixtures
from tests import test_company_model_cli as cli_fixtures


class HistoryFailureFeedbackTests(unittest.TestCase):
    def failure(self, *, eps=False):
        state, body, inputs = fixtures.case()
        if eps:
            body = fixtures.fixed(body)
            next(r for r in inputs['filed_lines'] if r['concept'] == 'synthetic:DilutedEPS')['cells']['2025-12-31']['value'] = '1.6'
        spec = specs.spec_from_response(state, json.dumps(body), decided_by='automation:test')
        spec = {**spec, 'spec_id': company_model_spec_id(spec['company_ref'], spec['state_hash'], spec['task_hash'])}
        before = copy.deepcopy(inputs)
        with self.assertRaises(FinancialStatementStructureError) as caught:
            materialize_financial_statement_structure(spec, inputs)
        self.assertEqual(inputs, before)
        return caught.exception

    def test_sum_error_retains_original_matching_message_and_full_differences(self):
        error = self.failure()
        self.assertEqual(str(error), 'formula operating does not tie to filed history')
        detail, hint = describe_failure(error)
        self.assertEqual(len(detail['mismatches']), 4)
        self.assertEqual(detail['mismatches'][0]['calculated'], '210')
        self.assertEqual(detail['mismatches'][0]['filed'], '190')
        self.assertEqual(detail['all_mismatches_hash'], content_hash(detail['mismatches']))
        self.assertFalse(detail['financial_evidence'])
        self.assertFalse(detail['retry_authority'])
        self.assertIn('210', hint)
        self.assertIn('190', hint)

    def test_eps_error_names_only_failed_period_without_relaxing_validation(self):
        error = self.failure(eps=True)
        detail, hint = describe_failure(error)
        self.assertEqual(detail['operator'], 'divide')
        self.assertEqual(len(detail['mismatches']), 1)
        self.assertEqual(detail['mismatches'][0]['period_end'], '2025-12-31')
        self.assertIn('2025-12-31', hint)
        self.assertIn('1.6', hint)

    def test_unrelated_error_or_changed_detail_does_not_claim_checked_difference(self):
        self.assertEqual(describe_failure(ValueError('failure')), (None, ''))
        error = self.failure()
        error.diagnostics['mismatches'][0]['filed'] = 'invented'
        self.assertEqual(describe_failure(error), (None, ''))


class HistoryFailurePlannerPathTests(unittest.TestCase):
    setUp = cli_fixtures.ChooseCompanyTests.setUp

    def test_actual_cli_refusal_reaches_checked_planner_ledger_without_new_retry(self):
        from dalton_core.company_model_cli import run_model_spec, choose_company
        from dalton_core.lane_failure_ledger import LaneFailureLedger
        from dalton_core.model_execution_failure_feedback import last_failures
        from datetime import datetime, timezone
        _, state = choose_company(self.missions, self.mission)
        body = cli_fixtures._spec_body()
        # Use the actual validator's EPS mismatch from the independent numeric
        # fixture. This test covers its propagation through the CLI and ledger.
        failure = HistoryFailureFeedbackTests().failure(eps=True)
        calls = []
        class Model:
            def __init__(inner, config, **kwargs): inner.config = config
            def call(inner, **kwargs):
                calls.append(kwargs)
                return {'text': json.dumps(body), 'work_order_ref': 'work:test-history',
                        'cost_micros': 11, 'replayed': False}
        config = self.state_dir / 'model.json'
        config.write_text(json.dumps({'structured_output_repair': {'max_attempts': 1}}))
        with patch('dalton_core.company_model_cli.CockpitModel', Model), patch(
                'dalton_core.company_model_cli.validate_financials_for_repair', side_effect=failure):
            summary = run_model_spec(state_dir=self.state_dir, model_config_path=config,
                summary_dir=self.state_dir / 'summary', scheduler_db=self.state_dir / 'scheduler.sqlite',
                company_ref=cli_fixtures.ACN, expected_state_hash=state['state_hash'],
                expected_task_hash=specs.TASK_HASH,
                expected_repair_policy_hash=content_hash({'max_attempts': 1}))
        self.assertEqual(summary['spec_status'], 'refused')
        self.assertEqual(len(calls), 1)
        self.assertEqual(summary['cost_micros'], 11)
        self.assertEqual(self.missions.company_model_specs(cli_fixtures.ACN), [])
        self.assertIn('2025-12-31', summary['failure_reason'])
        self.assertIn('1.5', summary['failure_reason'])
        self.assertIn('1.6', summary['failure_reason'])
        ledger = LaneFailureLedger(self.state_dir / 'lane-failure-ledger.sqlite')
        self.addCleanup(ledger.close)
        ledger.append_event(lane='mission_model_spec', item_key=cli_fixtures.ACN + '|exact-input',
            event='terminal', failure_class='content_refused', reason=summary['failure_reason'],
            rule='lane_content_refusal', status='refused', at=datetime.now(timezone.utc))
        feedback = last_failures(self.store.connection, state_dir=self.state_dir, company_ref=cli_fixtures.ACN)
        self.assertEqual(feedback['items'][0]['reason'], summary['failure_reason'])
        self.assertFalse(feedback['items'][0]['financial_evidence'])
        self.assertFalse(feedback['items'][0]['retry_authority'])
