from __future__ import annotations

import unittest

from dalton_core.connector_quota_policy import (
    apply_call_quota_to_limits,
    governed_daily_call_quota,
    governed_daily_call_quotas,
)


class ConnectorQuotaPolicyTests(unittest.TestCase):
    def test_owner_approved_daily_limits_are_exact(self) -> None:
        self.assertEqual(
            governed_daily_call_quotas(),
            [
                {
                    "connector_slug": "alphaengine",
                    "operation": "get_document",
                    "call_limit": 80,
                    "window_seconds": 86_400,
                    "reset_timezone": "Asia/Shanghai",
                },
                {
                    "connector_slug": "alphaengine",
                    "operation": "search_library",
                    "call_limit": 50,
                    "window_seconds": 86_400,
                    "reset_timezone": "Asia/Shanghai",
                },
                {
                    "connector_slug": "gemini-web-search",
                    "operation": "search_web",
                    "call_limit": 1_000,
                    "window_seconds": 86_400,
                    "reset_timezone": "Asia/Shanghai",
                },
            ],
        )

    def test_metric_limits_scale_from_call_ceiling(self) -> None:
        quota = governed_daily_call_quota("alphaengine", "get_document")
        self.assertEqual(
            apply_call_quota_to_limits(
                quota, max_response_bytes=2_000_000, max_records=1
            ),
            {
                "calls": 80,
                "bytes": 160_000_000,
                "records": 80,
                "cost_micros": 0,
            },
        )

    def test_unknown_route_has_no_implicit_unlimited_policy(self) -> None:
        with self.assertRaises(ValueError):
            governed_daily_call_quota("alphaengine", "arbitrary_tool")


if __name__ == "__main__":
    unittest.main()
