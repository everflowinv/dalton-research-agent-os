import unittest
from unittest.mock import patch
from dalton_core.investment_memo_repair_budget import MemoRepairBudget


class Model:
    def call(self, **kwargs):
        return {'work_order_ref': 'work:paid', 'cost_micros': 1, 'text': 'reply'}


class RepairBudgetTests(unittest.TestCase):
    def budget(self):
        return MemoRepairBudget(Model(), run_micros=70, producer_micros=14, verifier_micros=14)

    def test_settled_savings_allow_one_repair_while_reserving_remaining_calls(self):
        budget = self.budget()
        with patch('dalton_core.investment_memo_repair_budget.settled_call',
                   return_value={'accounted_micros': 1}):
            budget.call(purpose='investment_memo', mission={})
            self.assertFalse(budget.can_repair(remaining_groups=3))
            budget.call(purpose='investment_memo', mission={})
            self.assertTrue(budget.can_repair(remaining_groups=2))
        self.assertEqual(budget.accounted_micros, 2)

    def test_missing_proof_retains_full_reserve_despite_low_reported_cost(self):
        budget = self.budget()
        with patch('dalton_core.investment_memo_repair_budget.settled_call', side_effect=ValueError):
            for _ in range(2):
                budget.call(purpose='investment_memo', mission={})
        self.assertEqual(budget.accounted_micros, 28)
        self.assertFalse(budget.can_repair(remaining_groups=2))

    def test_fee_above_call_cap_is_never_clipped(self):
        budget = self.budget()
        with patch('dalton_core.investment_memo_repair_budget.settled_call',
                   return_value={'accounted_micros': 50}):
            budget.call(purpose='investment_memo', mission={})
        self.assertEqual(budget.accounted_micros, 50)
        self.assertFalse(budget.can_repair(remaining_groups=0))
