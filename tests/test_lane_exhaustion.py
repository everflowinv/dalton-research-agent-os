import unittest

from dalton_core.lane_exhaustion import exhausted_by_failures


class LaneExhaustionTests(unittest.TestCase):
    def test_all_current_remains_idle(self):
        result = exhausted_by_failures(
            [{"company_ref": "company:A", "reason": "current"}],
            success_reason="everything is current",
        )
        self.assertEqual(result, {"status": "idle", "reason": "everything is current"})

    def test_permission_holds_are_not_described_as_current(self):
        result = exhausted_by_failures(
            [{"company_ref": f"company:{index}", "reason": "not_permitted",
              "failure_class": "not_permitted"} for index in range(5)],
            success_reason="everything is current",
        )
        self.assertEqual(result["status"], "held")
        self.assertEqual(result["blocked_count"], 5)
        self.assertEqual(result["failure_counts"], {"not_permitted": 5})
        self.assertNotIn("current", result["reason"])


if __name__ == "__main__":
    unittest.main()
