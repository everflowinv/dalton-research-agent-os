"""Configured comparison series must reach the same bounded price collector."""
import copy
import json
import sqlite3
from pathlib import Path
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from dalton_core.mission_market_price_lane import (
    MissionMarketPriceLaneCoordinator, benchmark_universe,
)
from tests.test_mission_market_price_lane import (
    ACN, CTSH, NOW, FakeAuthority, FakeLauncher, mission, version,
)

SPY = "company:benchmark:SPY"


class BenchmarkCollectionTests(unittest.TestCase):
    def lane(self, *, params=None, policy=None):
        self.params = mission() if params is None else params
        self.policy = policy or {"abnormal_move": {"benchmark_refs": [SPY]}}
        self.launcher = FakeLauncher()
        self.authority = FakeAuthority({
            ref: version(last=NOW.date().isoformat()) for ref in (ACN, CTSH)
        })
        return MissionMarketPriceLaneCoordinator(
            authority=self.authority, launcher=self.launcher,
            mission=lambda: self.params, clock=lambda: NOW,
            benchmark_policy=lambda: self.policy,
        )

    def test_missing_benchmark_is_collected_once_without_mutating_coverage(self):
        lane = self.lane()
        original = copy.deepcopy(self.params)
        result = lane.dispatch_once()
        self.assertEqual(result["company_ref"], SPY)
        self.assertEqual(result["ticker"], "SPY")
        self.assertEqual(result["window_kind"], "backfill")
        self.assertEqual(lane.dispatch_once()["status"], "busy")
        self.assertEqual(len(self.launcher.started), 1)
        self.assertEqual(self.params, original)

    def test_current_benchmark_needs_no_call(self):
        lane = self.lane()
        self.authority.latest[SPY] = version(last=NOW.date().isoformat())
        lane.dispatch_once()
        self.assertEqual(self.launcher.started, [])

    def test_grant_and_nonempty_coverage_remain_required(self):
        for params in [mission(may_write=[]), mission(universe=[])]:
            with self.subTest(params=params):
                self.lane(params=params).dispatch_once()
                self.assertEqual(self.launcher.started, [])

    def test_invalid_or_conflicting_identity_fails_before_call(self):
        for refs in [["SPY"], ["company:sec-cik:123"], ["company:benchmark:../SPY"], SPY]:
            with self.subTest(refs=refs):
                lane = self.lane(policy={"abnormal_move": {"benchmark_refs": refs}})
                self.assertEqual(lane.dispatch_once()["status"], "misconfigured")
                self.assertEqual(self.launcher.started, [])
        lane = self.lane(params=mission(universe=[{"company_ref": SPY, "ticker": "QQQ"}]))
        self.assertEqual(lane.dispatch_once()["status"], "misconfigured")
        self.assertEqual(self.launcher.started, [])

    def test_policy_change_and_duplicates_have_no_stale_fetch(self):
        lane = self.lane()
        self.policy["abnormal_move"]["benchmark_refs"] = []
        lane.dispatch_once()
        self.assertEqual(self.launcher.started, [])
        rows = benchmark_universe({"abnormal_move": {"benchmark_refs": [SPY, SPY]}})
        self.assertEqual([r["company_ref"] for r in rows], [SPY])

    def test_writer_dispatch_reads_the_configured_tracking_policy(self):
        from dalton_core.mission_market_price_lane import dispatch
        from dalton_core.workspace_runtime_setup import TRACKING_POLICY

        self.lane()
        with tempfile.TemporaryDirectory() as temporary:
            policy_path = Path(temporary) / "tracking-policy.json"
            policy_path.write_text(json.dumps(TRACKING_POLICY))
            launchers = {
                "market_price_launcher": self.launcher,
                "tracking_launcher": SimpleNamespace(policy_path=policy_path),
            }
            connection = SimpleNamespace(execute=lambda *a: SimpleNamespace(
                fetchone=lambda: {"mission_version_id": "fixture-mission"}))
            server = SimpleNamespace(
                lane_launcher=launchers.get, lane_state={},
                store=SimpleNamespace(connection=connection),
                coverage_mission=SimpleNamespace(mission=lambda ref: self.params),
            )
            with patch("dalton_core.market_price.MarketPriceSeriesAuthority", return_value=self.authority), patch(
                "dalton_core.market_proxy_claim.MarketProxyClaimAuthority", return_value=None
            ), patch("dalton_core.market_proxy_claim.load_mapping_config", return_value={
                "mappings": [], "derive_from_model_drivers": False
            }), patch.object(MissionMarketPriceLaneCoordinator, "_today", return_value=NOW.date()):
                result = dispatch(server, {})
            self.assertEqual(result["company_ref"], SPY)

    def test_ask_never_compares_returns_with_different_endpoints(self):
        from dalton_core.ask_context import move_block
        core = sqlite3.connect(":memory:")
        self.addCleanup(core.close)
        core.row_factory = sqlite3.Row
        core.execute("CREATE TABLE market_price_series_versions (version_id TEXT, record_json TEXT, "
                     "company_ref TEXT, ticker TEXT, version_number INTEGER)")
        def save(ref, ticker, days):
            core.execute("INSERT INTO market_price_series_versions VALUES(?,?,?,?,1)", (
                ref, json.dumps({"bars": [{"date": d, "close": c} for d, c in days]}), ref, ticker))
        save(ACN, "ACN", [("2026-10-07", "100"), ("2026-10-08", "101"), ("2026-10-09", "110")])
        for days, expected in [
            ([("2026-10-07", "100"), ("2026-10-08", "102")], None),
            ([("2026-10-06", "100"), ("2026-10-09", "102")], None),
            ([("2026-10-07", "100"), ("2026-10-09", "102")], {"ticker": "SPY", "percent": 2.0}),
        ]:
            core.execute("DELETE FROM market_price_series_versions WHERE company_ref=?", (SPY,))
            save(SPY, "SPY", days)
            result = move_block(core, companies=[ACN], universe=[ACN],
                dates={"date_from": "2026-10-08", "date_to": "2026-10-09"},
                windows={}, kind="fact", label=lambda ref: "ACN", today="2026-10-10")
            self.assertEqual(result["rows"][0]["detail"]["benchmark"], expected)


if __name__ == "__main__":
    unittest.main()
