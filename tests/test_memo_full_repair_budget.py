"""The optional single redo has room without changing any call or paid door."""
import json
import unittest
from unittest.mock import patch

from dalton_core.call_budget import default_run_budget, resolve_run_budget, freeze_shared_budget_policy
from dalton_core.cockpit_model import CockpitModelError
from dalton_core.investment_memo_draft import GROUPS, MAX_REPAIRS
from dalton_core.investment_memo_repair_budget import MemoRepairBudget
from dalton_core.workspace_manager import set_shared_model_budget
from dalton_core.workspace import WorkspaceError
from tests import test_global_model_budget_limits as global_fixture
from tests import test_investment_memo_timeout_recovery as paid_fixture


class MemoRunDefaultsTests(unittest.TestCase):
    def test_default_reserves_four_groups_one_redo_and_verifier_but_explicit_low_cap_stays_low(self):
        self.assertEqual(default_run_budget('investment_memo'), {'max_cost_usd': 84.0, 'max_units': 4})
        self.assertEqual(len(GROUPS), 4)
        self.assertEqual(MAX_REPAIRS, 1)
        self.assertEqual(resolve_run_budget({}, 'investment_memo', defaults={'max_cost_usd': 1})['max_cost_usd'], 6 * 14)
        low = resolve_run_budget({'purpose_run_budgets': {'investment_memo': {'max_cost_usd': 70}}},
                                'investment_memo', defaults={'max_cost_usd': 84})
        self.assertEqual(low, {'max_cost_usd': 70, 'max_units': 4})

    def test_official_shared_cas_reaches_existing_and_future_configs_without_altering_calls(self):
        fixture = global_fixture.GlobalModelBudgetLimitsTests()
        fixture.setUp(); self.addCleanup(fixture.doCleanups)
        manager = fixture.manager()
        old = global_fixture.successor(fixture.policy, 'investment_memo',
                                     {'max_cost_usd': 70, 'max_units': 4}, 'run')
        fixture.install(old)
        configs = [{'shared_call_budget_policy_path': str(fixture.path),
                    'purpose_run_budgets': {'investment_memo': {'max_cost_usd': 70}}} for _ in range(5)]
        frozen = freeze_shared_budget_policy(configs[0])
        result = set_shared_model_budget(manager.path, 'owner@example.com', 'investment_memo',
                                         {'max_cost_usd': 84}, old['content_hash'], kind='run')
        changed = result['policy']
        for key in old:
            if key not in {'content_hash', 'revision', 'prior_hash', 'updated_at', 'actor_ref', 'purpose_run_limits'}:
                self.assertEqual(changed[key], old[key])
        self.assertEqual(changed['purpose_run_limits']['investment_memo'], {'max_cost_usd': 84, 'max_units': 4})
        for config in configs + [{'shared_call_budget_policy_path': str(fixture.path)}]:
            self.assertEqual(resolve_run_budget(config, 'investment_memo', defaults={'max_cost_usd': 1})['max_cost_usd'], 84)
        self.assertEqual(resolve_run_budget(frozen, 'investment_memo', defaults={'max_cost_usd': 1})['max_cost_usd'], 70)
        with self.assertRaises(WorkspaceError):
            set_shared_model_budget(manager.path, 'owner@example.com', 'investment_memo',
                                    {'max_cost_usd': 98}, old['content_hash'], kind='run')
        self.assertEqual(json.loads(fixture.path.read_text()), changed)

    def test_first_group_contract_errors_use_only_one_existing_repair_then_stop(self):
        from types import SimpleNamespace
        from tests import test_investment_memo_group_early_validation as early
        from tests import test_investment_memo_draft as draft
        from dalton_core.investment_memo_cli import run_memo
        case = early.EarlyMemoValidationTests()
        producer = draft.FakeModel([case.bad(), case.bad()])
        verifier = draft.FakeModel([])
        budget = MemoRepairBudget(producer, run_micros=84_000_000,
                                  producer_micros=14_000_000, verifier_micros=14_000_000)
        with patch('dalton_core.investment_memo_repair_budget.settled_call',
                   return_value={'accounted_micros': 100_000}):
            result = run_memo(store=SimpleNamespace(), frozen=case.frozen(), model=budget,
                              verifier_model=verifier, max_repairs=1, repair_budget=budget)
        self.assertEqual(result['status'], 'refused')
        self.assertEqual(len(producer.calls), 2)
        self.assertEqual(len(result['repairs']), 1)
        self.assertEqual(len(result['findings']), 2)
        self.assertEqual(verifier.calls, [])
        self.assertEqual(budget.accounted_micros, 200_000)


class PaidMemoHeadroomTests(unittest.TestCase):
    def setUp(self):
        paid_fixture.MemoTimeoutRecoveryTests.setUp(self)
        self.fixture = paid_fixture.MemoTimeoutRecoveryTests

    def kw(self, index, company=None):
        return self.fixture.kw(self, index, company)

    def fee_rows(self):
        return self.fixture.fee_rows(self)

    def wrapper(self):
        wrapper = MemoRepairBudget(self.model, run_micros=84_000_000,
                                   producer_micros=14_000_000, verifier_micros=14_000_000)
        wrapper.begin_stage(max_units=4, allow_technical=False)
        return wrapper

    def test_four_paid_groups_free_replay_preserves_all_historical_costs_for_two_companies(self):
        for company in ('company:one', 'company:two'):
            first = self.wrapper()
            for index in range(4):
                first.call(**self.kw(index, company))
            rows = self.fee_rows(); count = len(self.adapter.served)
            replay = self.wrapper()
            for index in range(4):
                result = replay.call(**self.kw(index, company))
                self.assertEqual(result['cost_micros'], 0)
            self.assertEqual(len(self.adapter.served), count)
            self.assertEqual(self.fee_rows(), rows)
            self.assertEqual(replay.accounted_micros, first.accounted_micros)
            self.assertGreater(replay.accounted_micros, 0)
            self.assertTrue(replay.can_repair(remaining_groups=0))

    def test_first_paid_free_replay_has_full_redo_room_and_keeps_old_bill(self):
        first = self.wrapper(); first.call(**self.kw(0))
        rows = self.fee_rows(); count = len(self.adapter.served)
        replay = self.wrapper(); answer = replay.call(**self.kw(0))
        self.assertEqual(answer['cost_micros'], 0)
        self.assertGreater(replay.accounted_micros, 0)
        self.assertEqual(replay.accounted_micros, first.accounted_micros)
        self.assertTrue(replay.can_repair(remaining_groups=3))
        replay.run_micros = 70_000_000
        self.assertFalse(replay.can_repair(remaining_groups=3))
        self.assertEqual(self.fee_rows(), rows)
        self.assertEqual(len(self.adapter.served), count)

    def test_unknown_timeout_replay_keeps_reserve_and_cannot_add_child_after_semantic_slot(self):
        first = self.wrapper(); first.call(**self.kw(0)); first.call(**self.kw(1))
        self.adapter.script = {'profile:gpt-6-astra': {'code': 'TIMEOUT', 'message': 'host timeout'}}
        with self.assertRaises(CockpitModelError): first.call(**self.kw(2))
        rows = self.fee_rows(); count = len(self.adapter.served)
        self.clock.advance(61); self.adapter.script = {}
        resumed = self.wrapper()
        resumed.call(**self.kw(0)); resumed.call(**self.kw(1))
        # run_memo disables technical recovery after its sole semantic redo.
        resumed.begin_stage(max_units=4, allow_technical=False)
        with self.assertRaises(CockpitModelError): resumed.call(**self.kw(2))
        self.assertGreaterEqual(resumed.accounted_micros, 14_000_000)
        self.assertEqual(len(self.adapter.served), count)
        self.assertEqual(self.fee_rows(), rows)


if __name__ == '__main__':
    unittest.main()
