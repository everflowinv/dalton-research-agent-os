from __future__ import annotations

import unittest
import tempfile
from pathlib import Path

from dalton_core.document_extraction import extraction_scheduler_policy, validate_transport_retry
from dalton_core.model_router import ModelRouter
from tests.test_transcript_polish_model_worker import profile, policy


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
        self.assertEqual(policy["max_lease_seconds"], 2 * 3 * (600 + 90) + 2 * 2 * 7 + 30)
        self.assertEqual(policy["max_total_lease_seconds"], 2 * policy["max_lease_seconds"])
        self.assertIn(str(policy["max_lease_seconds"]), policy["policy_version_id"])

    def test_long_configured_queue_and_backoff_are_bound_into_scheduler_authority(self):
        retry = validate_transport_retry({"max_definitely_not_sent_retries": 5,
                                          "queue_wait_seconds": 7200,
                                          "retry_backoff_seconds": 120})
        bound = extraction_scheduler_policy({"transport_retry": retry})
        self.assertEqual(bound["max_lease_seconds"], 6 * (600 + 7200) + 5 * 120 + 30)
        self.assertIn(str(bound["max_lease_seconds"]), bound["policy_version_id"])
        for field in retry:
            with self.subTest(field=field):
                with self.assertRaises(ValueError):
                    validate_transport_retry({**retry, field: True})
        with self.assertRaises(ValueError):
            validate_transport_retry({**retry, "queue_wait_seconds": 10 ** 100})

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

    def test_one_slot_three_profiles_sizes_three_full_fallbacks(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            database = Path(directory) / "router.sqlite"
            base = profile()
            profiles = []
            with ModelRouter(database) as router:
                for number in range(3):
                    item = dict(base)
                    item.update(id=f"profile:chain-{number}",
                                profile_version_ref=f"profile-version:chain-{number}:1",
                                model=f"chain-{number}")
                    # Deliberately one shared credential slot for all profiles.
                    router.register_profile(item); profiles.append(item["id"])
                route_policy = policy()
                route_policy.update(
                    id="model-routing-policy:three",
                    policy_version_ref="model-routing-policy-version:three:1",
                    purpose_overrides={"document_extraction": {
                        "mode": "explicit", "chain": profiles}})
                router.register_policy(route_policy)
                registered = router.get_policy(route_policy["policy_version_ref"])
                config = {
                    "model_router_db": str(database),
                    "routing_policy_ref": registered["policy_version_ref"],
                    "credential_slot_refs": [base["credential_slot_ref"]],
                    "transport_retry": {"max_definitely_not_sent_retries": 0,
                                        "queue_wait_seconds": 10,
                                        "retry_backoff_seconds": 0},
                }
                bound = extraction_scheduler_policy(config)
            self.assertEqual(bound["max_lease_seconds"], 3 * (600 + 10) + 30)
            self.assertIn(registered["content_hash"][:16], bound["policy_version_id"])


if __name__ == "__main__":
    unittest.main()
