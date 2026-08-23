from __future__ import annotations

import unittest

from dalton_core.connector_quota_policy import (
    apply_governed_quota_to_limits,
    governed_daily_quota,
    governed_daily_quotas,
)


class ConnectorQuotaPolicyTests(unittest.TestCase):
    def test_owner_approved_daily_limits_are_exact(self) -> None:
        self.assertEqual(
            governed_daily_quotas(),
            [
                {
                    "connector_slug": "alphaengine",
                    "operation": "get_document",
                    "quota_unit": "document",
                    "daily_unit_limit": 80,
                    "max_physical_calls_per_unit": 20,
                    "window_seconds": 86_400,
                    "reset_timezone": "Asia/Shanghai",
                },
                {
                    "connector_slug": "alphaengine",
                    "operation": "search_library",
                    "quota_unit": "search",
                    "daily_unit_limit": 50,
                    "max_physical_calls_per_unit": 1,
                    "window_seconds": 86_400,
                    "reset_timezone": "Asia/Shanghai",
                },
                {
                    "connector_slug": "gemini-web-search",
                    "operation": "search_web",
                    "quota_unit": "search",
                    "daily_unit_limit": 1_000,
                    "max_physical_calls_per_unit": 1,
                    "window_seconds": 86_400,
                    "reset_timezone": "Asia/Shanghai",
                },
            ],
        )

    def test_document_unit_limit_is_separate_from_page_safety_limits(self) -> None:
        quota = governed_daily_quota("alphaengine", "get_document")
        self.assertEqual(
            apply_governed_quota_to_limits(
                quota, max_response_bytes=2_000_000, max_records=1
            ),
            {
                "calls": 1_600,
                "bytes": 3_200_000_000,
                "records": 80,
                "cost_micros": 0,
            },
        )

    def test_unknown_route_has_no_implicit_unlimited_policy(self) -> None:
        with self.assertRaises(ValueError):
            governed_daily_quota("alphaengine", "arbitrary_tool")


if __name__ == "__main__":
    unittest.main()
