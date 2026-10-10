"""Obsolete specifications wait; validated execution gaps request original material."""
import copy
import tempfile
import unittest
from decimal import Decimal
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from dalton_core.company_model_forecast_cli import (
    ForecastSpecificationPending, obsolete_driver_specification, pending_companies)
from dalton_core.company_model_spec import TASK_HASH
from dalton_core.forecast_financial_execution import execute_financial_model, validate_execution_contract
from dalton_core.material_requests import open_requests, request
from dalton_core.mission_model_forecast_lane import MissionModelForecastLaneCoordinator
from dalton_core.model_forecast_driver import ForecastDriverExecutionRefused, ForecastModelValidationError
from tests import test_company_model_forecast as child_fixture
from tests.test_forecast_financial_execution import economic_model


def obsolete_spec(company='company:synthetic'):
    return {'company_ref': company, 'spec_id': 'company-model-spec:synthetic',
            'content_hash': 'b'*64, 'task_hash': 'a'*64,
            'revenue_anchor_concept': 'revenue',
            'revenue_drivers': [{'ref': 'segment', 'kind': 'segment', 'basis_concept': 'segment'}],
            'driver_formulas': []}


def refused_execution():
    body, spec = economic_model('sum')
    for driver in body['drivers']:
        if driver['ref'] == 'economic:a':
            for cell in driver['history']:
                cell['value'] = str(Decimal(cell['value']) - 1)
    execute_financial_model(body, spec)
    try:
        validate_execution_contract(body, publishing=True)
    except ForecastDriverExecutionRefused as exc:
        return body, exc
    raise AssertionError('unclosed source sum must refuse publication')


class SpecificationWaitTests(unittest.TestCase):
    def test_only_known_obsolete_incomplete_specs_wait(self):
        spec = obsolete_spec()
        wait = obsolete_driver_specification(spec)
        self.assertEqual(wait['spec_ref'], spec['spec_id'])
        self.assertEqual(wait['missing_driver_refs'], ['segment'])
        self.assertEqual(wait['required_task_hash'], TASK_HASH)
        for task_hash in (None, 'unknown', TASK_HASH):
            self.assertIsNone(obsolete_driver_specification({**spec, 'task_hash': task_hash}))
        self.assertIsNone(obsolete_driver_specification({**spec, 'driver_formulas': [{'driver_refs': ['segment']}]}))

    def test_legacy_single_total_identity_remains_executable(self):
        spec = obsolete_spec()
        spec['revenue_drivers'] = [{'ref': 'total', 'kind': 'mix', 'basis_concept': 'revenue'}]
        self.assertIsNone(obsolete_driver_specification(spec))

    def test_wait_never_reads_inputs_or_opens_material_or_launches(self):
        spec = obsolete_spec()
        missions = SimpleNamespace(company_model_specs=lambda: [spec],
                                   latest_company_model_spec=lambda _: spec)
        mission = {'universe': [{'company_ref': spec['company_ref']}]}
        with tempfile.TemporaryDirectory() as state:
            models = SimpleNamespace(latest=lambda _: self.fail('no model read for obsolete spec'))
            launcher = SimpleNamespace(state_dir=state, start=lambda **_: self.fail('must not launch'))
            lane = MissionModelForecastLaneCoordinator(missions=missions, models=models,
                launcher=launcher, mission=lambda: mission, failure_ledger_dir=state)
            with patch('dalton_core.company_model_forecast_cli.build_model_inputs') as inputs:
                for _ in range(2):
                    outcome = lane.dispatch_once()
                    self.assertEqual(outcome['reason_code'], 'model_specification_pending')
                    self.assertEqual(outcome['waiting_specifications'][spec['company_ref']]['spec_hash'], 'b'*64)
                inputs.assert_not_called()
            self.assertEqual(open_requests(state), [])

    def test_obsolete_first_company_does_not_block_qualified_other_company(self):
        old = obsolete_spec('company:a')
        good = {**obsolete_spec('company:b'), 'task_hash': TASK_HASH}
        specs = {'company:a': old, 'company:b': good}
        missions = SimpleNamespace(company_model_specs=lambda: list(specs.values()),
                                   latest_company_model_spec=lambda ref: specs[ref])
        models = SimpleNamespace(latest=lambda _: None, connection=None)
        waiting = {}
        with patch('dalton_core.company_model_forecast_cli.build_model_inputs', return_value={'synthetic': True}) as inputs, \
             patch('dalton_core.company_model_forecast_cli.forecast_reporting_boundary', return_value=None), \
             patch('dalton_core.company_model_forecast_cli.needs_model', return_value='first'):
            pending = pending_companies(missions, models,
                {'universe': [{'company_ref': ref} for ref in specs]}, waiting_specifications=waiting)
        self.assertEqual([p[0] for p in pending], ['company:b'])
        self.assertEqual(set(waiting), {'company:a'})
        inputs.assert_called_once_with(missions, good)


class ExecutionGapTests(unittest.TestCase):
    def test_exact_replayed_refusal_keeps_report_and_invalid_report_still_refuses(self):
        body, exc = refused_execution()
        self.assertIsInstance(exc, ForecastModelValidationError)
        self.assertEqual(exc.driver_report, body['execution_contract']['drivers'])
        self.assertEqual(exc.contract_hash, body['execution_contract']['content_hash'])
        tampered = copy.deepcopy(body)
        tampered['execution_contract']['drivers']['status'] = 'executed'
        with self.assertRaisesRegex(ForecastModelValidationError, 'hash differs'):
            validate_execution_contract(tampered, publishing=True)
        valid, _ = economic_model('sum')
        validate_execution_contract(valid, publishing=True)

    def harness(self):
        harness = child_fixture.LaneStateTests('test_the_first_run_publishes_a_model_and_its_forecast_lines')
        harness.setUp()
        self.addCleanup(harness.doCleanups)
        return harness

    def test_child_preserves_refusal_and_existing_material_authority_idempotency(self):
        harness = self.harness()
        body, exc = refused_execution()
        from dalton_core.model_forecast_driver import ForecastModelAuthority
        with patch('dalton_core.company_model_forecast_cli.run_company_forecast', side_effect=exc):
            first = harness.child()
            second = harness.child()
        self.assertEqual(first['forecast_status'], 'refused:unexecuted_driver_plan')
        self.assertEqual(first['driver_execution'], body['execution_contract']['drivers'])
        self.assertEqual(first['execution_contract_hash'], exc.contract_hash)
        self.assertIn(str(exc), first['failure_reason'])
        self.assertEqual(first['formal_authority_writes'], 0)
        self.assertEqual(first['cost_micros'], 0)
        self.assertEqual(ForecastModelAuthority(harness.store).versions(child_fixture.ACN), [])
        requests = open_requests(harness.state_dir)
        self.assertEqual(len(requests), 1)
        self.assertEqual(second['material_requests'][0]['write_status'], 'duplicate')
        held = request(harness.state_dir, requests[0]['id'])
        self.assertEqual(held['subject'], child_fixture.ACN)
        self.assertIn('complete consolidation bridge', held['need']['question'])
        self.assertIn('no residual', held['need']['question'])
        self.assertEqual(held['source_priority'], ['indexed_corpus', 'sec_edgar', 'alphaengine', 'web'])

    def test_direct_child_reports_spec_wait_not_generic_input_refusal(self):
        harness = self.harness()
        wait = {child_fixture.ACN: obsolete_driver_specification(obsolete_spec(child_fixture.ACN))}
        with patch('dalton_core.company_model_forecast_cli.choose_company', side_effect=ForecastSpecificationPending(wait)), \
             patch('dalton_core.company_model_forecast_cli.run_company_forecast') as run:
            summary = harness.child()
        self.assertEqual(summary['forecast_status'], 'awaiting_specification')
        self.assertEqual(summary['waiting_specifications'], wait)
        self.assertEqual(summary['formal_authority_writes'], 0)
        run.assert_not_called()
