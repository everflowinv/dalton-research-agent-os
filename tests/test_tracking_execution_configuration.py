"""Installed tracking limits reach scheduling and actual event writes."""
import json
from datetime import timedelta
from unittest.mock import patch

from dalton_core.mission_tracking_lane import MissionTrackingLaneCoordinator
from dalton_core.tracking_cadence import load_policy, TrackingCadenceValidationError
from dalton_core.tracking_lane_cli import run_tracking, price_events, company_events
from tests.test_mission_tracking_lane import FakeLauncher, NOW, POLICY_PATH
from tests.p14a_fixtures import ACN, P14aHarness


class TrackingExecutionTests(P14aHarness):
    def policy(self, **changes):
        wire = json.loads(POLICY_PATH.read_text())
        wire["execution"] = {"interval_seconds": 60, "max_events_per_run": 1,
                             "max_candidates_per_source": 120, "lookback_days": 30}
        wire["execution"].update(changes)
        path = self.state_dir / "configured-tracking.json"
        path.write_text(json.dumps(wire))
        return path

    def test_installed_cap_and_explicit_override_leave_remaining_events_eligible(self):
        self.pass_screen(ACN)
        for index in range(3):
            self.claim(statement=f"ACN source statement {index}")
        path = self.policy()
        first = run_tracking(state_dir=self.state_dir, summary_dir=self.state_dir / "summary",
                             policy_path=path, now=NOW)
        second = run_tracking(state_dir=self.state_dir, summary_dir=self.state_dir / "summary",
                              policy_path=path, now=NOW, max_events=5)
        self.assertEqual(first["events_recorded"], 1)
        self.assertEqual(second["events_recorded"], 2)
        self.assertEqual(first["effective_limits"]["max_candidates_per_source"], 120)

    def test_configured_cadence_and_policy_change_rekey_schedule(self):
        launcher = FakeLauncher()
        launcher.policy_path = self.policy()
        coordinator = MissionTrackingLaneCoordinator(launcher=launcher,
                         mission=lambda: self.mission, clock=lambda: NOW)
        first = coordinator.window_ref(self.mission)
        coordinator.clock = lambda: NOW + timedelta(seconds=61)
        self.assertNotEqual(first, coordinator.window_ref(self.mission))
        coordinator.clock = lambda: NOW
        self.policy(max_events_per_run=2)
        self.assertNotEqual(first, coordinator.window_ref(self.mission))

    def test_malformed_execution_refused(self):
        for value in (True, 0, -1, "120"):
            with self.subTest(value=value), self.assertRaises(TrackingCadenceValidationError):
                load_policy(self.policy(max_events_per_run=value))

    def test_existing_price_lookback_setting_reaches_detector(self):
        class Prices:
            def series(self, ref): return []
        policy = load_policy(self.policy())
        policy["abnormal_move"]["max_lookback_trading_days"] = 9
        with patch("dalton_core.tracking_lane_cli.recent_settled_dates", return_value=[]) as dates:
            price_events(Prices(), self.mission, policy, tracked=[ACN])
        self.assertEqual(dates.call_args.kwargs["limit"], 9)

    def test_configured_scan_limit_reaches_disclosure_sources(self):
        with patch("dalton_core.tracking_lane_cli.buyback_event_candidates", return_value={"events": []}) as buybacks, patch("dalton_core.tracking_lane_cli.trading_plan_event_candidates", return_value={"events": []}) as plans:
            company_events(self.store, self.mission, company_ref=ACN, now=NOW, max_candidates_per_source=123)
        self.assertEqual(buybacks.call_args.kwargs["limit"], 123)
        self.assertEqual(plans.call_args.kwargs["limit"], 123)
