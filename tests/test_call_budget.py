import math
import unittest

from dalton_core.call_budget import (
    CallBudgetError, budget_fingerprint, default_call_budget,
    resolve_call_budget, validate_budget_overrides,
)


DEFAULTS = {
    "max_input_tokens": 100,
    "max_output_tokens": 20,
    "max_cost_usd": 0.5,
    "timeout_seconds": 30,
}


class CallBudgetTests(unittest.TestCase):
    def test_purpose_overrides_general_overrides_defaults(self):
        config = {
            "call_budget": {"max_input_tokens": 80, "max_cost_usd": 0.4},
            "purpose_call_budgets": {
                "judge": {"max_input_tokens": 60, "timeout_seconds": 10},
                "verifier": {"max_output_tokens": 7},
            },
        }
        self.assertEqual(resolve_call_budget(config, "judge", defaults=DEFAULTS), {
            "max_input_tokens": 60, "max_output_tokens": 20,
            "max_cost_usd": 0.4, "timeout_seconds": 10,
        })
        self.assertEqual(resolve_call_budget(config, "verifier", defaults=DEFAULTS), {
            "max_input_tokens": 80, "max_output_tokens": 7,
            "max_cost_usd": 0.4, "timeout_seconds": 30,
        })

    def test_invalid_values_and_unknown_fields_fail_closed(self):
        invalid = [
            {"call_budget": {"max_cost_usd": math.inf}},
            {"call_budget": {"max_input_tokens": True}},
            {"call_budget": {"timeout_seconds": 0}},
            {"call_budget": {"dollars": 1}},
            {"purpose_call_budgets": {"Not Valid": {"max_output_tokens": 1}}},
        ]
        for config in invalid:
            with self.subTest(config=config), self.assertRaises(CallBudgetError):
                resolve_call_budget(config, "judge", defaults=DEFAULTS)

    def test_fingerprint_is_canonical_and_changes_with_a_limit(self):
        first = resolve_call_budget({}, "judge", defaults=DEFAULTS)
        reordered = dict(reversed(list(first.items())))
        self.assertEqual(budget_fingerprint(first), budget_fingerprint(reordered))
        changed = {**first, "max_output_tokens": 21}
        self.assertNotEqual(budget_fingerprint(first), budget_fingerprint(changed))

    def test_public_override_validator_keeps_a_partial_value_partial(self):
        self.assertEqual(validate_budget_overrides({"max_cost_usd": 0.2}),
                         {"max_cost_usd": 0.2})


if __name__ == "__main__":
    unittest.main()
