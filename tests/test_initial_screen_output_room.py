"""Initial-screen output headroom preserves USD authority and frozen batches."""
import json
import unittest

from dalton_core.call_budget import (
    budget_fingerprint, default_call_budget, freeze_shared_budget_policy,
    resolve_call_budget,
)
from dalton_core.cockpit_model import CockpitModel, build_work, register_purpose
from dalton_core.shared_call_budget_policy import load_shared_call_budget_policy
from dalton_core.store import content_hash
from dalton_core.workspace_manager import _write, set_shared_model_budget
from tests import test_workspace_manager as manager_fixture
from tests import test_global_model_budget_limits as shared_fixture


class InitialScreenOutputRoomTests(unittest.TestCase):
    def test_real_caller_defaults_give_new_environment_more_output_only(self):
        # The STRL caller still supplies its historical 16K constructor fallback.
        # The packaged purpose default must reach the actual Cockpit resolver.
        model = object.__new__(CockpitModel)
        model.config = {}
        model.max_input_tokens = 600000
        model.max_output_tokens = 16000
        model.max_cost_usd = None
        model.timeout_seconds = 900
        self.assertEqual(model.budget_for('initial_screen'), {
            'max_input_tokens': 600000, 'max_output_tokens': 64000,
            'max_cost_usd': 14.0, 'timeout_seconds': 900,
        })
        self.assertEqual(default_call_budget('initial_screen_verifier'), {
            'max_input_tokens': 600000, 'max_output_tokens': 16000,
            'max_cost_usd': 14.0, 'timeout_seconds': 900,
        })

    def test_official_global_cas_reaches_existing_future_consumers_not_old_batch(self):
        fixture = manager_fixture.WorkspaceManagerTests()
        fixture.setUp()
        self.addCleanup(fixture.doCleanups)
        path = fixture.root / 'shared.json'
        policy = shared_fixture.legacy_policy()
        path.write_text(json.dumps(policy)); path.chmod(0o600)
        fixture.config['shared_call_budget_policy_path'] = str(path)
        _write(fixture.path, fixture.config)
        first = set_shared_model_budget(fixture.path, 'owner@example.com', 'initial_screen',
            {'max_output_tokens': 16000}, policy['content_hash'])['policy']
        configs = [{'shared_call_budget_policy_path': str(path),
                    'purpose_call_budgets': {'initial_screen': {'max_output_tokens': 16000}}},
                   {'shared_call_budget_policy_path': str(path)}]
        frozen = freeze_shared_budget_policy(configs[0])
        defaults = default_call_budget('initial_screen')
        # Publishing code does not override an existing explicitly saved cap.
        old_budget = resolve_call_budget(frozen, 'initial_screen', defaults=defaults)
        self.assertEqual(old_budget['max_output_tokens'], 16000)
        saved = set_shared_model_budget(fixture.path, 'owner@example.com', 'initial_screen',
            {'max_output_tokens': 64000}, first['content_hash'])['policy']
        self.assertEqual(load_shared_call_budget_policy(path), saved)
        for config in configs:
            actual = resolve_call_budget(config, 'initial_screen', defaults=defaults)
            self.assertEqual(actual, {**old_budget, 'max_output_tokens': 64000})
        self.assertEqual(resolve_call_budget(frozen, 'initial_screen', defaults=defaults), old_budget)
        for key in ('default_max_cost_usd', 'purpose_max_cost_usd', 'purpose_run_limits'):
            self.assertEqual(saved[key], first[key])
        self.assertEqual(saved['prior_hash'], first['content_hash'])
        self.assertEqual(saved['purpose_call_limits']['initial_screen'], {'max_output_tokens': 64000})

    def test_same_request_new_budget_has_new_work_identity_and_old_replay_is_exact(self):
        register_purpose('initial_screen')
        old_budget = {'max_input_tokens': 600000, 'max_output_tokens': 16000,
                      'max_cost_usd': 14.0, 'timeout_seconds': 900}
        new_budget = {**old_budget, 'max_output_tokens': 64000}
        def work(budget):
            return build_work(purpose='initial_screen', request_id='synthetic-screen:document:0',
                prompt='Read only the registered filing evidence.',
                mission_version_ref='coverage-mission-version:synthetic:1',
                created_at='2026-10-11T00:00:00+00:00',
                max_input_tokens=budget['max_input_tokens'],
                max_output_tokens=budget['max_output_tokens'], max_cost_usd=budget['max_cost_usd'],
                max_seconds=budget['timeout_seconds'], budget_identity=budget_fingerprint(budget))
        old = work(old_budget)
        before = old.to_dict()
        new = work(new_budget)
        self.assertNotEqual(new.id, old.id)
        self.assertEqual(work(old_budget).to_dict(), before)
        self.assertEqual(old.to_dict(), before)
        self.assertEqual(new.budget['max_total_tokens'], 664000)
        self.assertEqual({k:v for k,v in new.budget.items() if k not in {'max_output_tokens', 'max_total_tokens'}},
                         {k:v for k,v in old.budget.items() if k not in {'max_output_tokens', 'max_total_tokens'}})
        self.assertNotEqual(content_hash(new.to_dict()), content_hash(before))


if __name__ == '__main__':
    unittest.main()
