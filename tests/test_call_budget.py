import math
import json
import unittest
from pathlib import Path

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

    def test_every_packaged_single_call_cost_default_is_one_dollar(self):
        path = Path(__file__).parents[1] / "src" / "dalton_core" / "call_budget_defaults.json"
        wire = json.loads(path.read_text())
        self.assertEqual(wire["defaults"]["max_cost_usd"], 1.0)
        self.assertTrue(wire["purposes"])
        # Batch C2: the whole-pack purposes carry the long-context floor
        # (call_budget.LONG_CONTEXT_CALL_FLOOR), which every stored config and
        # the shared cost policy are held to; every other purpose is 1 USD.
        from dalton_core.call_budget import LONG_CONTEXT_CALL_FLOOR, LONG_CONTEXT_PURPOSES

        self.assertEqual(
            {name: value["max_cost_usd"] for name, value in wire["purposes"].items()},
            {name: (LONG_CONTEXT_CALL_FLOOR["max_cost_usd"] if name in LONG_CONTEXT_PURPOSES else 1.0)
             for name in wire["purposes"]},
        )
        for name in LONG_CONTEXT_PURPOSES:
            self.assertEqual(wire["purposes"][name], LONG_CONTEXT_CALL_FLOOR)

    def test_long_form_brain_drafts_leave_room_for_reasoning(self):
        # 2026-09-29: glm-5.3 (a brain-tier link) spent all 4096 output tokens
        # of 从已登记原文起草定向回答 reasoning and returned no text -- a paid
        # call. The two long-form drafting stages on the brain chain get 16k;
        # at the priciest brain link (claude-opus-5, $4/$20 per million) a
        # 64k-in/16k-out call is still ~$0.58, inside the $1 per-call ceiling.
        for purpose in ("mission_directed_document_draft",
                        "registered_annual_report_draft"):
            with self.subTest(purpose=purpose):
                budget = default_call_budget(purpose)
                self.assertGreaterEqual(budget["max_output_tokens"], 16000)
                worst = (budget["max_input_tokens"] * 4.0
                         + budget["max_output_tokens"] * 20.0) / 1_000_000
                self.assertLessEqual(worst, budget["max_cost_usd"])

    def test_structured_research_drafts_resolve_reasoning_headroom_without_changing_other_caps(self):
        from dalton_core.cockpit_model import CockpitModel
        purposes = {
            "thesis_reflection": (60_000, 180, 1_500),
            "earnings_calibration": (60_000, 240, 2_000),
            "earnings_preview": (60_000, 240, 4_000),
            "conviction_call": (60_000, 240, 3_000),
        }
        model = CockpitModel.__new__(CockpitModel)
        model.config = {}
        model.max_input_tokens = 60_000
        model.max_output_tokens = 1_500  # Old caller fallbacks are not the catalogue.
        model.max_cost_usd = 1.0
        model.timeout_seconds = 180
        for purpose, (input_cap, timeout, _) in purposes.items():
            with self.subTest(purpose=purpose):
                effective = model.budget_for(purpose)
                self.assertEqual(effective, {
                    "max_input_tokens": input_cap, "max_output_tokens": 16_384,
                    "max_cost_usd": 1.0, "timeout_seconds": timeout,
                })

    def test_structured_draft_headroom_preserves_saved_override_and_verifier_limits(self):
        verifier_caps = {
            "thesis_reflection": 1_500, "earnings_calibration": 2_000,
            "earnings_preview": 2_000, "conviction_call": 3_000,
        }
        for purpose, cap in verifier_caps.items():
            with self.subTest(purpose=purpose):
                self.assertEqual(default_call_budget(purpose + "_verifier")["max_output_tokens"], cap)
                config = {"purpose_call_budgets": {purpose: {"max_output_tokens": 2_048}}}
                budget = resolve_call_budget(config, purpose, defaults=DEFAULTS)
                self.assertEqual(budget["max_output_tokens"], 2_048)
                self.assertEqual(budget["max_cost_usd"], 1.0)

    def test_run_budget_resolves_general_then_purpose(self):
        config = {
            "run_budget": {"max_cost_usd": 4.0, "max_units": 3},
            "purpose_run_budgets": {"unlisted_runtime": {"max_units": 2, "max_calls": 5}},
        }
        self.assertEqual(resolve_run_budget(
            config, "unlisted_runtime", defaults={"max_cost_usd": 5.0, "max_units": 4}),
            {"max_cost_usd": 4.0, "max_units": 2, "max_calls": 5})

    def test_run_budget_rejects_unknown_nonpositive_and_nonfinite(self):
        for value in ({"other": 1}, {"max_events": 0},
                      {"max_calls": True}, {"max_cost_usd": math.nan}):
            with self.subTest(value=value), self.assertRaises(CallBudgetError):
                validate_run_budget_overrides(value)

    def test_packaged_run_budget_overrides_legacy_defaults(self):
        self.assertEqual(resolve_run_budget(
            {}, "dossier", defaults={"max_cost_usd": 9.0, "max_units": 9}),
            {"max_cost_usd": 56.0, "max_units": 3})


if __name__ == "__main__":
    unittest.main()
