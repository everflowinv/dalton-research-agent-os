import math
import unittest

from dalton_core.call_budget import (
    CallBudgetError, budget_fingerprint, default_call_budget,
    resolve_call_budget, resolve_run_budget, validate_budget_overrides,
    validate_run_budget_overrides,
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

    def test_unlisted_purpose_keeps_its_callers_legacy_defaults(self):
        self.assertEqual(default_call_budget("unlisted_runtime", defaults=DEFAULTS),
                         DEFAULTS)

    def test_catalogued_event_budget_replaces_caller_legacy_defaults(self):
        self.assertEqual(default_call_budget("event_judgement", defaults=DEFAULTS), {
            "max_input_tokens": 60_000, "max_output_tokens": 1_500,
            "max_cost_usd": 1.0, "timeout_seconds": 180,
        })

    def test_run_budget_resolves_general_then_purpose(self):
        config = {
            "run_budget": {"max_cost_usd": 4.0, "max_units": 3},
            "purpose_run_budgets": {"dossier": {"max_units": 2, "max_calls": 5}},
        }
        self.assertEqual(resolve_run_budget(
            config, "dossier", defaults={"max_cost_usd": 5.0, "max_units": 4}),
            {"max_cost_usd": 4.0, "max_units": 2, "max_calls": 5})

    def test_run_budget_rejects_unknown_nonpositive_and_nonfinite(self):
        for value in ({"other": 1}, {"max_events": 0},
                      {"max_calls": True}, {"max_cost_usd": math.nan}):
            with self.subTest(value=value), self.assertRaises(CallBudgetError):
                validate_run_budget_overrides(value)


if __name__ == "__main__":
    unittest.main()
