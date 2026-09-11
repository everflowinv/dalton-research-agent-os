import sqlite3
import unittest

from dalton_core.model_forecast_driver import ForecastModelAuthority
from dalton_core.research_financial_state import financial_state
from dalton_core.research_state import company_state
from dalton_core.store import DaltonStore
from tests.test_model_forecast_driver import ACN, model


class ResearchFinancialStateTests(unittest.TestCase):
    def setUp(self):
        self.store = DaltonStore(":memory:")
        self.authority = ForecastModelAuthority(self.store)
        self.mission = {"id": "mission:current"}

    def tearDown(self):
        self.store.close()

    def test_zero_document_figures_preserves_existing_model_history(self):
        stored = self.authority.publish(model(mission_version_ref=self.mission["id"]))
        self.store.connection.execute("PRAGMA query_only=ON")
        result = financial_state(self.store.connection, ACN, self.mission)
        state = company_state({"company_ref": ACN}, figures={"total": 0},
                              financial_model=result)
        self.assertEqual(state["figures"]["total"], 0)
        self.assertEqual(result["model_content_hash"], stored["content_hash"])
        self.assertEqual(result["history_quarters"], 4)
        self.assertGreater(result["drivers_with_history"], 0)
        self.assertTrue(result["current_mission"])
        self.assertEqual(result["stage_completion"], "not_assessed")
        self.assertTrue(all("history_points" in d for d in result["driver_history"]))

    def test_historical_model_is_available_but_not_current(self):
        self.authority.publish(model(mission_version_ref="mission:old"))
        result = financial_state(self.store.connection, ACN, self.mission)
        self.assertEqual(result["status"], "available")
        self.assertFalse(result["current_mission"])

    def test_absent_company_is_missing_not_borrowed_from_other_company(self):
        self.authority.publish(model())
        result = financial_state(self.store.connection, "company:other", self.mission)
        self.assertEqual(result, {"status": "missing", "reason": "no_forecast_model"})

    def test_missing_table_does_not_install_or_claim_zero_history(self):
        connection = sqlite3.connect(":memory:")
        try:
            connection.execute("PRAGMA query_only=ON")
            result = financial_state(connection, ACN, self.mission)
            self.assertEqual(result["status"], "unavailable")
            self.assertNotIn("history_quarters", result)
            self.assertEqual(connection.execute("SELECT count(*) FROM sqlite_master").fetchone()[0], 0)
        finally:
            connection.close()

    def test_invalid_model_is_unknown_not_available_or_zero(self):
        from unittest.mock import patch
        with patch.object(ForecastModelAuthority, "latest", side_effect=ValueError("drift")):
            result = financial_state(self.store.connection, ACN, self.mission)
        self.assertEqual(result["status"], "unavailable")
        self.assertEqual(result["reason"], "model_authority_read_failed")
        self.assertNotIn("history_quarters", result)


if __name__ == "__main__":
    unittest.main()
