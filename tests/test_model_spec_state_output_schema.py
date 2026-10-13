"""Finite source-specific shape, including the real paid repair boundary."""
import copy
import json
import unittest
from datetime import timedelta
from unittest.mock import patch

from dalton_core import company_model_spec as specs, company_model_cli as cli
from dalton_core import model_spec_complete_diagnostics as complete
from dalton_core import model_spec_joint_field_diagnostics as joint
from dalton_core.cockpit_model import CockpitModelError
from dalton_core.store import content_hash
from dalton_core.scheduler import Scheduler
from tests import test_model_spec_joint_field_diagnostics as fixtures
from tests import test_operating_driver_handoff as handoff
from tests import test_period_source_family as source_families
from dalton_core.period_source_family import catalog as source_family_catalog
from tests.test_company_model_cli import _spec_body
from dalton_core.company_model_state import build_company_model_state


class StateOutputSchemaTests(unittest.TestCase):
    def test_finite_four_shapes_are_the_actual_prompt_not_a_relaxation(self):
        state, _, _ = fixtures.case()
        original = copy.deepcopy(specs.OUTPUT_SCHEMA)
        for operating in (False, True):
            for families in (False, True):
                value = copy.deepcopy(state)
                if operating:
                    value['operating_driver_context'] = {}
                if families:
                    filings, rows = source_families.fixture(value['company_ref'])
                    value.setdefault('numeric_context', {})['period_source_family_catalog'] = source_family_catalog(value['company_ref'], filings, rows)
                schema = specs.output_schema_for_state(value)
                self.assertEqual(specs.checked_current_output_schema(schema), schema)
                self.assertEqual(specs.repair_output_schema_from_prompt(specs.build_prompt(value)), schema)
                fields = schema['properties']['revenue_drivers']['items']['properties']
                self.assertEqual('basis_operating_metric_ref' in fields, operating)
                self.assertEqual('basis_period_family_refs' in fields, families)
        self.assertEqual(specs.OUTPUT_SCHEMA, original)
        self.assertIs(specs.output_schema_for_state(state), specs.OUTPUT_SCHEMA)

    def test_unknown_or_relaxed_schema_is_not_an_authenticated_current_contract(self):
        state, _, _ = fixtures.case()
        state['operating_driver_context'] = {}
        for change in ('unknown', 'required', 'additional', 'minimum'):
            schema = copy.deepcopy(specs.output_schema_for_state(state))
            props = schema['properties']['revenue_drivers']['items']['properties']
            if change == 'unknown':
                props['foreign_ref'] = {'type': 'string'}
            elif change == 'required':
                schema['required'].remove('assessment')
            elif change == 'additional':
                schema['additionalProperties'] = True
            else:
                props['basis_operating_metric_ref']['minLength'] = 0
            with self.subTest(change=change), self.assertRaises(specs.CompanyModelSpecError):
                specs.checked_current_output_schema(schema)
            with self.subTest(change=change), self.assertRaises(specs.CompanyModelSpecError):
                specs.repair_output_schema_from_prompt('OUTPUT_SCHEMA:\n' + json.dumps(schema))

    def test_dynamic_diagnostics_preserve_refs_and_require_the_exact_root_schema(self):
        state, bad, fixed = fixtures.case()
        state['operating_driver_context'] = {}
        filings, rows = source_families.fixture(state['company_ref'])
        state.setdefault('numeric_context', {})['period_source_family_catalog'] = source_family_catalog(state['company_ref'], filings, rows)
        state['state_hash'] = content_hash({k: v for k, v in state.items() if k != 'state_hash'})
        for body in (bad, fixed):
            body['revenue_drivers'][0]['basis_operating_metric_ref'] = 'metric:reported'
            body['revenue_drivers'][0]['basis_period_family_refs'] = ['source-family:original']
        schema = specs.output_schema_for_state(state)
        report = joint.from_state(bad, state)
        self.assertEqual(report['status'], 'complete')
        self.assertEqual(report['output_schema'], schema)
        joint.assert_only_allowed_changed(bad, fixed, report)
        joint.validate_frozen_parent(bad, report, state_hash=state['state_hash'], root_prompt=specs.build_prompt(state))
        changed = copy.deepcopy(fixed)
        changed['revenue_drivers'][0]['basis_operating_metric_ref'] = 'metric:foreign'
        with self.assertRaises(specs.CompanyModelSpecError):
            joint.assert_only_allowed_changed(bad, changed, report)
        old_state = copy.deepcopy(state)
        old_state.pop('operating_driver_context')
        with self.assertRaises(ValueError):
            joint.validate_frozen_parent(bad, report, state_hash=state['state_hash'], root_prompt=specs.build_prompt(old_state))
        independent = copy.deepcopy(bad)
        independent['expense_lines'] = copy.deepcopy(fixed['expense_lines'])
        diagnostics = complete.diagnose(independent, output_schema=schema)
        self.assertEqual(diagnostics['status'], 'complete')
        self.assertEqual(len(diagnostics['findings']), 2)
        complete.validate_parent(independent, diagnostics)

    def operating_fixture(self):
        source = handoff.StrongOperatingHandoffTests('runTest')
        source.addCleanup = self.addCleanup
        f, owner, origin_model, context, input_spec = source.completed_context()
        state = build_company_model_state(owner._missions, context['company_ref'], ticker='TEST', operating_driver_context=context)
        fixed = _spec_body()
        fixed['financial_statement_structure']['schema_version'] = '0.6'
        for line in fixed['financial_statement_structure']['lines']:
            line['annual_forecast_method'] = None
        fixed['revenue_drivers'] = [{k: v for k, v in d.items() if k not in {
            'basis_operating_context_hash', 'basis_operating_context_evidence_refs'}} for d in input_spec['revenue_drivers']]
        fixed['driver_formulas'] = [{'output_ref': 'revenue', 'operator': 'product',
            'driver_refs': [d['ref'] for d in fixed['revenue_drivers']],
            'because': 'Explicit synthetic economics; the reported quantity times reported price.'}]
        fixed['expense_lines'][0]['driver_ref'] = fixed['revenue_drivers'][0]['ref']
        bad = copy.deepcopy(fixed)
        bad['expense_lines'][0].pop('because')
        runtime = fixtures.JointAdmissionTests('runTest')
        runtime.addCleanup = self.addCleanup
        fixture = runtime.fixture(state_body=(state, bad), outputs=[bad, fixed], mission=f.mission)
        source.readonly_files(f, owner, origin_model)
        return source, f, owner, fixture

    def test_actual_scheduler_operating_repair_full_finance_restart_free_same_slot(self):
        _, source, owner, fixture = self.operating_fixture()
        f, state, bad, _, model, adapter, root = fixture
        args = dict(state=state, mission=f.mission, original_call=root,
            repair_config={'max_attempts': 1}, decided_by='automation:test')
        checked = []
        def full(spec):
            checked.append(owner._missions.validate_company_model_spec_financials(spec))
        accepted, repairs, paid = cli._validated_spec_with_repair(model=model, financial_validator=full, **args)
        self.assertEqual(len(adapter.served), 2)
        self.assertEqual(len(checked), 1)
        self.assertEqual(len(checked[0]['financial_inputs']['operating_driver_lines']), 2)
        self.assertIn('basis_operating_metric_ref', accepted['revenue_drivers'][0])
        with Scheduler(model.scheduler_db) as store:
            tables = ('scheduler_work_orders', 'scheduler_formal_results', 'scheduler_result_envelopes')
            tables = [t for t in tables if store.connection.execute("SELECT 1 FROM sqlite_master WHERE name=?", (t,)).fetchone()]
            before = {t: [tuple(r) for r in store.connection.execute('SELECT * FROM ' + t)] for t in tables}
        model.clock = lambda: fixtures.runtime.NOW + timedelta(days=1)
        args['repair_config'] = {'max_attempts': 0}
        _, again, replay = cli._validated_spec_with_repair(model=model, financial_validator=full, **args)
        self.assertEqual(replay['work_order_ref'], paid['work_order_ref'])
        self.assertEqual(again[0]['cost_micros'], 0)
        self.assertTrue(again[0]['replayed'])
        self.assertEqual(len(adapter.served), 2)
        with Scheduler(model.scheduler_db) as store:
            self.assertEqual(before, {t: [tuple(r) for r in store.connection.execute('SELECT * FROM ' + t)] for t in tables})

    def test_rehashed_dynamic_schema_foreign_field_cannot_purchase(self):
        _, _, _, fixture = self.operating_fixture()
        f, state, bad, _, model, adapter, root = fixture
        real = model.call
        def tamper(**kwargs):
            binding = copy.deepcopy(kwargs['_structured_output_repair'])
            schema = binding['diagnostic_bundle']['output_schema']
            schema['properties']['revenue_drivers']['items']['properties']['invented'] = {'type': 'string'}
            kwargs['_structured_output_repair'] = binding
            return real(**kwargs)
        with patch.object(model, 'call', side_effect=tamper), self.assertRaises((CockpitModelError, specs.CompanyModelSpecError)):
            cli._validated_spec_with_repair(model=model, state=state, mission=f.mission,
                original_call=root, repair_config={'max_attempts': 1}, decided_by='automation:test')
        self.assertEqual(len(adapter.served), 1)


if __name__ == '__main__':
    unittest.main()
