"""A runtime status read must neither reconcile pending work nor hash an object."""

from contextlib import ExitStack
import sqlite3
import unittest
from unittest.mock import patch

from dalton_core.earnings_season_cli import earnings_window_input_fingerprint
from dalton_core.forecast_reconciliation import ForecastReconciliationAuthority
from dalton_core.store import DaltonStore
from tests.test_forecast_reconciliation import ForecastReconciliationFixture, OWNER


COMPANY = "company:synthetic:fingerprint"


class EarningsWindowFingerprintTests(unittest.TestCase):
    def setUp(self):
        self.store = DaltonStore(":memory:")
        self.addCleanup(self.store.close)
        self.fixture = ForecastReconciliationFixture(self.store)
        self.fixture.line("100", subject=COMPANY, line_ref="forecast-line:synthetic:revenue")
        self.fixture.claim(current="99000000", prior="98000000", subject=COMPANY)
        stack = self.enterContext(ExitStack())
        for name, value in {
            "company_theses": [], "recent_claims": [], "guidance_profile_for": None,
            "consensus_reader_for": None, "input_table_for": None,
            "market_view_rows": [], "_source_keys": [],
        }.items():
            stack.enter_context(patch("dalton_core.earnings_season_cli." + name, return_value=value))
        stack.enter_context(patch("dalton_core.model_forecast_driver.ForecastModelAuthority"))
        stack.enter_context(patch("dalton_core.research_event.ResearchEventAuthority"))
        # A serializable held forecast, independent of the model's migration.
        from dalton_core.model_forecast_driver import ForecastModelAuthority
        ForecastModelAuthority.return_value.latest.return_value = None
        self.reconcile = stack.enter_context(patch(
            "dalton_core.earnings_season_cli.reconcile_for",
            side_effect=AssertionError("a fingerprint must not reconcile")))

    def fingerprint(self):
        return earnings_window_input_fingerprint(
            self.store, None, {"id": "mission:test", "content_hash": "a" * 64},
            {"company_ref": COMPANY, "window": "calibration", "expected_date": "2026-09-01"})

    def test_pending_work_stays_pending_and_held_results_change_digest(self):
        changes = self.store.connection.total_changes
        self.store.connection.execute("PRAGMA query_only=ON")
        before = self.fingerprint()
        self.assertEqual(before, self.fingerprint())
        self.assertEqual(self.store.connection.total_changes, changes)
        self.assertEqual(ForecastReconciliationAuthority.read_existing(
            self.store.connection, company_ref=COMPANY), [])
        self.store.connection.execute("PRAGMA query_only=OFF")
        result = self.fixture.reconciler.reconcile_pending(requested_by=OWNER, mission_resolver=None)
        self.assertEqual(len(result["created"]), 1)
        self.store.connection.execute("PRAGMA query_only=ON")
        changes = self.store.connection.total_changes
        after = self.fingerprint()
        self.assertNotEqual(before, after)
        self.assertEqual(after, self.fingerprint())
        self.assertEqual(self.store.connection.total_changes, changes)
        self.reconcile.assert_not_called()

    def test_absent_ledger_needs_no_migration(self):
        connection = sqlite3.connect(":memory:")
        self.addCleanup(connection.close)
        connection.execute("PRAGMA query_only=ON")
        self.assertEqual(ForecastReconciliationAuthority.read_existing(
            connection, company_ref=COMPANY), [])
        self.assertEqual(connection.execute("SELECT count(*) FROM sqlite_master").fetchone()[0], 0)
