import tempfile
import unittest

from dalton_core.lane_failure_ledger import lane_budget
from dalton_core.mission_claim_index_lane import DRIVER_KEY as CLAIM
from dalton_core.mission_conviction_lane import DRIVER_KEY as CONVICTION
from dalton_core.mission_debate_map_lane import DRIVER_KEY as DEBATE
from dalton_core.mission_model_forecast_lane import DRIVER_KEY as FORECAST
from dalton_core.mission_model_spec_lane import DRIVER_KEY as SPEC
from dalton_core.mission_sensitivity_lane import DRIVER_KEY as SENSITIVITY


class FingerprintLaneBudgetTests(unittest.TestCase):
    def test_each_lane_replays_dependency_and_probes_the_same_digest(self):
        for lane in (CLAIM, DEBATE, CONVICTION, SPEC, FORECAST, SENSITIVITY):
            with self.subTest(lane=lane), tempfile.TemporaryDirectory() as state:
                item = "company:a|unchanged-business-digest"
                first = lane_budget(lane, state_dir=state)
                decision = first.record(
                    item, reason="model_unavailable", status="model_unavailable")
                self.assertEqual(decision.classification.failure_class,
                                 "dependency_unavailable")
                self.assertEqual(decision.failures, 0)

                restarted = lane_budget(lane, state_dir=state)
                # The first admitted attempt after restart is the real child
                # probe for this exact business digest.
                self.assertIsNone(restarted.blocked(item))
                self.assertEqual(restarted.clear(item), [item])
                self.assertIsNone(restarted.blocked(item))

    def test_items_and_permission_refusals_are_independent(self):
        budget = lane_budget(CLAIM)
        gated = budget.record(
            "company:a|digest", reason="gated:mission does not grant claim write")
        self.assertEqual(gated.action, "not_permitted")
        self.assertEqual(gated.failures, 0)
        self.assertIsNone(budget.blocked("company:b|digest"))


if __name__ == "__main__":
    unittest.main()
