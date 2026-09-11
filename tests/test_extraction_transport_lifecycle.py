from __future__ import annotations

import unittest

from dalton_core.document_extraction import extraction_scheduler_policy


class ExtractionTransportLifecycleTests(unittest.TestCase):
    def test_scheduler_lease_covers_every_queue_and_provider_try(self) -> None:
        config = {
            "purpose_call_budgets": {
                "document_extraction": {
                    "max_input_tokens": 64_000,
                    "max_output_tokens": 4_096,
                    "max_cost_usd": 1,
                    "timeout_seconds": 600,
                }
            },
            "transport_retry": {
                "max_definitely_not_sent_retries": 2,
                "queue_wait_seconds": 90,
                "retry_backoff_seconds": 7,
            },
            "capacity_retry": {"scheduler_max_attempts": 4},
        }
        policy = extraction_scheduler_policy(config)
        self.assertEqual(policy["max_attempts"], 4)
        # Two credential slots can each consume the full safe retry envelope.
        config["credential_slot_refs"] = ["slot:a", "slot:b"]
        policy = extraction_scheduler_policy(config)
        self.assertEqual(policy["max_lease_seconds"], 2 * 3 * (600 + 90) + 2 * 7 + 30)
        self.assertEqual(policy["max_total_lease_seconds"], 2 * policy["max_lease_seconds"])
        self.assertIn(str(policy["max_lease_seconds"]), policy["policy_version_id"])

    def test_longest_extraction_purpose_sizes_shared_scheduler(self) -> None:
        config = {
            "credential_slot_refs": ["slot:a"],
            "purpose_call_budgets": {
                "document_extraction": {"timeout_seconds": 60},
                "document_numeric_extraction": {"timeout_seconds": 700},
                "metric_discovery_extraction": {"timeout_seconds": 500},
            },
        }
        self.assertEqual(extraction_scheduler_policy(config)["max_lease_seconds"], 730)


if __name__ == "__main__":
    unittest.main()
