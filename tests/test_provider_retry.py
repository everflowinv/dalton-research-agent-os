from __future__ import annotations

import unittest

from dalton_core.provider_retry import (
    ProviderRetryError, returned_provider_failure_proof, validate_provider_retry,
)
from tests.test_transcript_polish_model_worker import FakeAdapter, NOW, candidate, profile
from dalton_core.contracts import WorkOrder


class ProviderRetryContractTests(unittest.TestCase):
    def test_policy_is_closed_and_non_negative(self):
        self.assertEqual(validate_provider_retry({
            "max_same_profile_retries": 1, "retry_backoff_seconds": 2,
        })["max_same_profile_retries"], 1)
        for bad in (
            {"max_same_profile_retries": -1, "retry_backoff_seconds": 0},
            {"max_same_profile_retries": 1, "retry_backoff_seconds": 0, "codes": []},
        ):
            with self.assertRaises(ProviderRetryError):
                validate_provider_retry(bad)
        self.assertEqual(validate_provider_retry({
            "max_same_profile_retries": 4, "retry_backoff_seconds": 0,
        })["max_same_profile_retries"], 4)
        policy = validate_provider_retry({
            "max_same_profile_retries": 1, "retry_backoff_seconds": 2,
            "unknown_recovery": {"max_fresh_work_orders": 2,
                                 "retry_backoff_seconds": 30,
                                 "max_elapsed_seconds": 600},
        })
        self.assertEqual(policy["unknown_recovery"]["max_fresh_work_orders"], 2)
        disabled = validate_provider_retry({
            **policy,
            "unknown_recovery": {**policy["unknown_recovery"], "max_fresh_work_orders": 0},
        })
        self.assertEqual(disabled["unknown_recovery"]["max_fresh_work_orders"], 0)
        for recovery in ({}, {"max_fresh_work_orders": -1,
                              "retry_backoff_seconds": 0,
                              "max_elapsed_seconds": 60}):
            with self.assertRaises(ProviderRetryError):
                validate_provider_retry({
                    "max_same_profile_retries": 1, "retry_backoff_seconds": 2,
                    "unknown_recovery": recovery,
                })

    def test_only_exact_completed_broker_failure_is_eligible(self):
        work = WorkOrder(
            schema_version="0.1", id="work:provider-retry-proof", created_at=NOW.isoformat(),
            updated_at=NOW.isoformat(), question="x", requested_capabilities=("research",),
            runtime_profile_ref="runtime:test", budget={"max_output_tokens": 1},
            idempotency_key="provider-retry-proof", declared_side_effects=(), status="ready",
            input_refs=(), metadata={},
        )
        invocation, result = FakeAdapter(candidate()).execute(work, {
            "id": "route:provider-retry-proof", "attempt_number": 1,
        }, profile())
        from dalton_core.contracts import ResultEnvelope
        def failed(code, source="openclaw-model-broker", mode="execute"):
            return ResultEnvelope(
                schema_version="0.1", id=result.id, created_at=result.created_at,
                work_order_ref=work.id, invocation_ref=invocation.id, status="failed",
                outputs={}, actual_side_effects=(), usage_refs=result.usage_refs,
                artifact_refs=(), error={"code": code, "source": source},
                metadata={"broker_response_hash": "a" * 64, "broker_request_mode": mode,
                          "route_decision_ref": "route:provider-retry-proof",
                          "dispatch_proof": {"authority": "openclaw-model-adapter",
                                             "state": "provider_completed_failure",
                                             "version": "0.1"}},
            )
        self.assertIsNotNone(returned_provider_failure_proof(
            invocation, failed("RATE_LIMITED")))
        for envelope in (
            failed("NOT_RATE_LIMITED"), failed("RATE_LIMITED", "adapter"),
            failed("RATE_LIMITED", mode="replay_only"),
        ):
            self.assertIsNone(returned_provider_failure_proof(invocation, envelope))


if __name__ == "__main__":
    unittest.main()
