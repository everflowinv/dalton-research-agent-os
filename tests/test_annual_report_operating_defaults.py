"""Installed role configs must admit a long read and more than one paid try."""

import unittest

from dalton_core.annual_report_runtime import plan_model_execution


class AnnualReportOperatingDefaultsTests(unittest.TestCase):
    def test_unconfigured_seed_has_long_call_and_retry_headroom_for_both_roles(self):
        config = {"routing_policy_ref": "policy:operator-choice:1",
                  "credential_slot_refs": ["credential-slot:operator-choice"]}
        for role in ("draft", "verifier"):
            with self.subTest(role=role):
                execution = plan_model_execution(
                    config, "registered_annual_report_" + role)
                self.assertEqual(execution["routing_policy_ref"], config["routing_policy_ref"])
                self.assertEqual(execution["credential_slot_refs"], config["credential_slot_refs"])
                self.assertGreaterEqual(execution["max_seconds"], 600)
                self.assertGreater(execution["max_attempts"], 1)
                self.assertGreaterEqual(execution["max_elapsed_seconds"],
                                        execution["max_seconds"] * execution["max_attempts"])

    def test_owner_can_raise_all_default_bounds_and_keep_role_specific_authority(self):
        config = {"routing_policy_ref": "policy:operator-choice:2",
                  "credential_slot_refs": ["credential-slot:operator-choice"],
                  "purpose_call_budgets": {"registered_annual_report_draft": {
                      "max_input_tokens": 240000, "max_output_tokens": 16000,
                      "max_cost_usd": 3.0, "timeout_seconds": 1800}},
                  "purpose_run_budgets": {"registered_annual_report_draft": {
                      "max_units": 12, "max_seconds": 30000}}}
        draft = plan_model_execution(config, "registered_annual_report_draft")
        self.assertEqual((draft["max_input_tokens"], draft["max_output_tokens"],
                          draft["max_cost_usd"], draft["max_seconds"],
                          draft["max_attempts"], draft["max_elapsed_seconds"]),
                         (240000, 16000, 3.0, 1800, 12, 30000))
        verifier = plan_model_execution(config, "registered_annual_report_verifier")
        self.assertNotEqual(verifier["max_attempts"], draft["max_attempts"])


if __name__ == "__main__":
    unittest.main()
