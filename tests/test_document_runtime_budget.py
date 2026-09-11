from __future__ import annotations

import unittest

from dalton_core.annual_report_runtime import AnnualReportRuntimeError, plan_model_execution


class DocumentRuntimeBudgetTests(unittest.TestCase):
    def config(self):
        return {"routing_policy_ref": "policy:test", "credential_slot_refs": ["credential:test"],
                "budget_db": "/fixture/budget.sqlite", "budget_policy_ref": "budget-policy:test",
                "call_budget": {"max_input_tokens": 64000, "max_output_tokens": 4096,
                                "max_cost_usd": 1.0, "timeout_seconds": 600},
                "provider_retry": {"max_same_profile_retries": 1, "retry_backoff_seconds": 2},
                "transport_retry": {"max_definitely_not_sent_retries": 1,
                                    "queue_wait_seconds": 600, "retry_backoff_seconds": 2}}

    def test_default_elapsed_covers_the_configured_queue_call_and_retry_windows(self):
        for purpose in ("mission_directed_document_draft", "mission_directed_document_verifier"):
            with self.subTest(purpose=purpose):
                config = self.config()
                config["run_budget"] = {"max_units": 4}
                execution = plan_model_execution(config, purpose)
                self.assertEqual(execution["max_seconds"], 600)
                self.assertGreaterEqual(execution["max_elapsed_seconds"], 4 * (2 * 1200 + 2))
                self.assertEqual(execution["max_attempts"], 4)

    def test_packaged_annual_elapsed_policy_remains_authoritative(self):
        config = self.config()
        config["run_budget"] = {"max_units": 4}
        execution = plan_model_execution(config, "registered_annual_report_draft")
        self.assertEqual(execution["max_elapsed_seconds"], 7200)

    def test_purpose_elapsed_override_takes_precedence(self):
        config = self.config()
        config["run_budget"] = {"max_units": 4, "max_seconds": 10000}
        config["purpose_run_budgets"] = {
            "mission_directed_document_draft": {"max_seconds": 3000}}
        execution = plan_model_execution(config, "mission_directed_document_draft")
        self.assertEqual(execution["max_elapsed_seconds"], 3000)

    def test_an_explicit_insufficient_elapsed_cap_remains_a_visible_error(self):
        config = self.config()
        config["run_budget"] = {"max_units": 4, "max_seconds": 600}
        with self.assertRaisesRegex(AnnualReportRuntimeError, "exceeds run max_seconds"):
            plan_model_execution(config, "mission_directed_document_draft")
