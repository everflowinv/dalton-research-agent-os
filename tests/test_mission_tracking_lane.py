"""P14a: the resident lane -- every tick, every covered company, no selection."""

from __future__ import annotations

import json
import subprocess
import sys
import textwrap
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from dalton_core.lane_child_launcher import LaneChildRejected
from dalton_core.lane_registry import LaunchAgentContext, lane_for_operation
from dalton_core.mission_tracking_lane import (
    LANE,
    LAUNCHER_KWARG,
    WINDOW_SECONDS,
    MissionTrackingLaneCoordinator,
    argv_fragment,
    build_launcher,
)
from dalton_core.research_event import ResearchEventAuthority
from dalton_core.tracking_cadence import POLICY_PATH
from dalton_core.tracking_lane_cli import (
    company_events,
    missing_write_scopes,
    price_events,
    round_robin,
    run_tracking,
)
from tests.p14a_fixtures import ACN, AUTOMATION, CTSH, DXC, EPAM, IBM, P14aHarness

NOW = datetime(2026, 9, 9, 12, 0, tzinfo=timezone.utc)


class FakeLauncher:
    def __init__(self, *, configured=True):
        self.configured = configured
        self.started: list[str] = []
        self.tickets: dict[str, dict] = {}
        self.error = None

    def start(self, *, window_ref):
        if not self.configured:
            raise LaneChildRejected("no tracking policy")
        if self.error is not None:
            raise self.error
        ticket = {"id": f"tracking-run:{len(self.started):024d}", "window_ref": window_ref,
                  "status": "running"}
        self.started.append(window_ref)
        self.tickets[ticket["id"]] = ticket
        return ticket

    def status(self, ticket_ref):
        return self.tickets[ticket_ref]

    def settle(self, ticket_ref, summary, status="succeeded"):
        self.tickets[ticket_ref] = {**self.tickets[ticket_ref], "status": status,
                                    "summary": summary}


class RegistrationTests(unittest.TestCase):
    def test_the_lane_is_registered_after_the_price_lane(self):
        self.assertEqual(LANE.operation, "dispatch_mission_tracking")
        self.assertEqual(LANE.driver_key, "mission_tracking")
        self.assertIs(lane_for_operation("dispatch_mission_tracking"), LANE)
        prices = lane_for_operation("dispatch_mission_market_prices")
        judgement = lane_for_operation("dispatch_event_judgement")
        self.assertLess(prices.order, LANE.order)
        self.assertLess(LANE.order, judgement.order)

    def test_a_fresh_interpreter_derives_the_lane_from_the_registry_alone(self):
        # The registry's whole promise: adding a lane is a line in LANE_MODULES
        # and the writer, the driver and the plist all see it.
        script = textwrap.dedent(
            """
            from dalton_core import writer_server
            from dalton_core.lane_registry import tick_lanes
            assert "dispatch_mission_tracking" in writer_server.CORE_OPERATIONS
            assert "dispatch_mission_tracking" in writer_server.CORE_DISCOVERY_OPERATIONS
            assert "dispatch_mission_tracking" in writer_server.OPERATION_FIELDS
            assert "dispatch_event_judgement" in writer_server.CORE_OPERATIONS
            keys = [spec.driver_key for spec in tick_lanes()]
            assert keys.index("mission_tracking") > keys.index("mission_market_prices")
            assert keys.index("event_judgement") > keys.index("mission_tracking")
            print("ok")
            """
        )
        root = Path(__file__).resolve().parents[1]
        result = subprocess.run(
            [sys.executable, "-c", script], capture_output=True, text=True,
            env={"PYTHONPATH": str(root / "src"), "PATH": "/usr/bin:/bin"},
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("ok", result.stdout)

    def test_the_policy_file_is_the_switch(self):
        import tempfile

        with tempfile.TemporaryDirectory() as name:
            state = Path(name)
            self.assertEqual(argv_fragment(LaunchAgentContext(state=state)), [])
            (state / "tracking-policy.json").write_text("{}", encoding="utf-8")
            self.assertEqual(
                argv_fragment(LaunchAgentContext(state=state)),
                ["--tracking-policy", str(state / "tracking-policy.json")],
            )

    def test_no_policy_argument_means_no_launcher(self):
        class Args:
            tracking_policy = None
            db = "/tmp/core.sqlite"

        self.assertIsNone(build_launcher(Args()))


class CoordinatorTests(unittest.TestCase):
    def setUp(self):
        self.launcher = FakeLauncher()
        self.now = NOW
        self.mission = {"id": "coverage-mission-version:us-it-services:1"}
        self.coordinator = MissionTrackingLaneCoordinator(
            launcher=self.launcher, mission=lambda: self.mission,
            clock=lambda: self.now,
        )

    def test_the_first_tick_launches_and_the_second_in_the_same_window_does_not(self):
        first = self.coordinator.dispatch_once()
        self.assertEqual(first["status"], "launched")
        second = self.coordinator.dispatch_once()
        self.assertEqual(second["status"], "idle")
        self.assertEqual(len(self.launcher.started), 1)

    def test_a_new_window_launches_again(self):
        self.coordinator.dispatch_once()
        self.launcher.settle(self.launcher.tickets and
                             next(iter(self.launcher.tickets)), {"events_recorded": 0})
        self.now = NOW + timedelta(seconds=WINDOW_SECONDS + 1)
        self.assertEqual(self.coordinator.dispatch_once()["status"], "launched")
        self.assertEqual(len(self.launcher.started), 2)

    def test_the_previous_child_is_settled_on_the_following_tick(self):
        launched = self.coordinator.dispatch_once()
        self.launcher.settle(launched["ticket_ref"], {
            "tracking_status": "recorded", "events_recorded": 3,
            "events_by_kind": {"news": 3}, "tracked_companies": [ACN],
        })
        self.now = NOW + timedelta(seconds=WINDOW_SECONDS + 1)
        result = self.coordinator.dispatch_once()
        self.assertEqual(result["settled"]["events_recorded"], 3)
        self.assertEqual(result["settled"]["tracking_status"], "recorded")

    def test_no_mission_is_unconfigured_not_a_crash(self):
        self.mission = None
        self.assertEqual(self.coordinator.dispatch_once()["status"], "unconfigured")

    def test_an_uninstalled_lane_is_rejected_with_its_reason(self):
        self.launcher.configured = False
        result = self.coordinator.dispatch_once()
        self.assertEqual(result["status"], "rejected")
        self.assertIn("tracking policy", result["reason"])


class GrantTests(P14aHarness):
    grants = ()

    def test_both_words_are_named_when_neither_is_granted(self):
        self.assertEqual(missing_write_scopes(self.mission), ["market_event"])

    def test_a_granted_mission_needs_nothing(self):
        self.grant("market_event", "observation")
        self.assertEqual(missing_write_scopes(self.mission), [])


class ChildTests(P14aHarness):
    def run_child(self, **kwargs):
        return run_tracking(
            state_dir=self.state_dir, summary_dir=self.state_dir / "summary",
            policy_path=POLICY_PATH, now=NOW, **kwargs,
        )

    def test_a_mission_with_no_passed_screen_tracks_nothing(self):
        summary = self.run_child()
        self.assertEqual(summary["status"], "idle")
        self.assertEqual(summary["tracking_status"], "nothing_tracked")

    def test_an_ungranted_mission_costs_one_query_and_says_so(self):
        params = dict(self.params)
        autonomy = dict(params["autonomy"])
        autonomy["may_write"] = [w for w in autonomy["may_write"]
                                 if w not in ("market_event", "observation")]
        params["autonomy"] = autonomy
        params.update({"version_id": "coverage-mission-version:us-it-services:30",
                       "prior_version_ref": self.mission["id"],
                       "idempotency_key": "coverage-mission:us-it-services:30"})
        self.missions.create_mission(self.mission_ref, **params)
        summary = self.run_child()
        self.assertEqual(summary["tracking_status"], "ungranted")
        self.assertIn("market_event", summary["failure_reason"])

    def test_documents_and_claims_become_events(self):
        self.pass_screen(ACN)
        self.claim(statement="ACN 说需求在改善。")
        summary = self.run_child()
        self.assertEqual(summary["status"], "succeeded")
        self.assertEqual(summary["tracking_status"], "recorded")
        self.assertEqual(summary["events_by_kind"], {"claim": 1})
        self.assertEqual(summary["tracked_companies"], [ACN])

    def test_a_second_run_records_nothing_new(self):
        self.pass_screen(ACN)
        self.claim()
        self.run_child()
        again = self.run_child()
        self.assertEqual(again["events_recorded"], 0)
        self.assertEqual(again["events_duplicate"], 1)
        self.assertEqual(again["tracking_status"], "no_new_events")

    def test_a_price_move_against_flat_peers_is_recorded(self):
        self.pass_screen(ACN)
        # Four peers, because the policy will not compute an excess against a
        # basket of fewer than three names.
        for ref, ticker in ((ACN, "ACN"), (CTSH, "CTSH"), (EPAM, "EPAM"),
                            (IBM, "IBM"), (DXC, "DXC")):
            closes = [("2026-09-01", "100"),
                      ("2026-09-02", "105" if ref == ACN else "100")]
            self.prices(ref, closes, ticker=ticker)
        summary = self.run_child()
        self.assertEqual(summary["events_by_kind"].get("price_move"), 1)
        events = ResearchEventAuthority(self.store).events(company_ref=ACN,
                                                           kind="price_move")
        self.assertEqual(events[0]["payload"]["excess_vs_basket_percent"], "5.0000")

    def test_every_tracked_company_is_scanned_on_every_run(self):
        # Not one per tick: five covered names looked at once every five ticks
        # is a queue, not daily tracking.
        self.pass_screen(ACN)
        self.pass_screen(CTSH)
        self.claim(subject=ACN)
        self.claim(subject=CTSH)
        summary = self.run_child()
        self.assertEqual(set(summary["tracked_companies"]), {ACN, CTSH})
        self.assertEqual(summary["events_recorded"], 2)

    def test_the_run_reports_what_each_source_is_due(self):
        self.pass_screen(ACN)
        summary = self.run_child()
        due = summary["due"][ACN]
        self.assertIn("alphaengine", due)
        self.assertTrue(due["alphaengine"]["due"])
        self.assertEqual(due["alphaengine"]["interval_seconds"], 43200)

    def test_a_dry_run_writes_nothing(self):
        self.pass_screen(ACN)
        self.claim()
        summary = self.run_child(dry_run=True)
        self.assertEqual(summary["events_recorded"], 0)
        self.assertEqual(summary["events_by_kind"], {"claim": 1})
        self.assertEqual(ResearchEventAuthority(self.store).counts(ACN), {})

    def test_a_company_that_has_not_passed_cannot_be_tracked_by_name(self):
        summary = self.run_child(company_ref=ACN)
        self.assertEqual(summary["tracking_status"], "not_tracked")

    def test_the_summary_is_always_written(self):
        self.run_child()
        summary = json.loads((self.state_dir / "summary" / "summary.json")
                             .read_text(encoding="utf-8"))
        self.assertEqual(summary["schema_version"], "0.1")
        self.assertEqual(summary["policy_ref"], "tracking-policy:p14a:v2")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()


class DivergenceRunTests(P14aHarness):
    """A window where the price ran against what our thesis implies."""

    def setUp(self):
        super().setUp()
        self.pass_screen(ACN)

    def thesis(self, company_ref="industry:us-it-services",
               thesis_ref="thesis:us-it-services:demand-bottoming"):
        """A human-admitted industry thesis, through the authority that admits them.

        Industry-level, like the live one: a thesis with no company subject
        covers every name in the industry, which is how a company with no
        thesis of its own still has something to diverge from.

        Written the long way round on purpose: a thesis row inserted by hand
        would not carry the admission decision the table's own CHECK requires,
        and a fixture that has to disable a constraint is a fixture that stops
        testing the thing.
        """

        from dalton_core.coverage_admission import CoverageAdmissionAuthority

        admission = self.state["admission"]
        assert isinstance(admission, CoverageAdmissionAuthority)
        candidate = admission.propose_thesis_admission(
            candidate_id=f"thesis-admission-candidate:{thesis_ref}",
            thesis_ref=thesis_ref,
            company_ref=company_ref,
            industry_ref="industry:us-it-services",
            template_ref="template:x",
            driver_refs=["driver:d"],
            mandate_version_ref=self.state["mandate"]["id"],
            mandate_version_hash=self.state["mandate"]["content_hash"],
            driver_pack_version_ref=self.state["pack"]["id"],
            driver_pack_version_hash=self.state["pack"]["content_hash"],
            content={
                "statement": "AI-led reinvention offsets soft discretionary consulting.",
                "mechanism": "Bookings lead revenue by two to four quarters.",
                "confidence": "medium",
                "implied_expectation": "Growth stabilises within a year.",
                "claim_refs": [],
                "catalyst_refs": ["catalyst:quarterly-results"],
                "falsifier_refs": ["falsifier:x"],
                "change_reason": "fixture",
            },
            actor_ref="human:coverage-owner",
            idempotency_key=f"thesis-admission-candidate:{thesis_ref}",
        )
        return admission.decide_thesis_admission(
            candidate_id=candidate["id"],
            candidate_hash=candidate["content_hash"],
            verdict="admit",
            rationale="The thesis names its drivers and its falsifiers.",
            decision_id=f"thesis-admission-decision:{thesis_ref}",
            actor_ref="human:portfolio-manager",
            idempotency_key=f"thesis-admission-decision:{thesis_ref}",
        )

    def window(self, acn_last, peers_last="100"):
        dates = [f"2026-09-{day:02d}" for day in range(1, 11)]
        captured = "2026-09-10T23:30:00+00:00"
        for ref, ticker in ((ACN, "ACN"), (CTSH, "CTSH"), (EPAM, "EPAM"),
                            (IBM, "IBM"), (DXC, "DXC")):
            last = acn_last if ref == ACN else peers_last
            closes = [(date, "100") for date in dates[:-1]] + [(dates[-1], last)]
            self.prices(ref, closes, ticker=ticker, captured_at=captured)

    def run_child(self, **kwargs):
        return run_tracking(
            state_dir=self.state_dir, summary_dir=self.state_dir / "summary",
            policy_path=POLICY_PATH, now=datetime(2026, 9, 11, 2, 0,
                                                  tzinfo=timezone.utc),
            **kwargs,
        )

    def test_a_long_thesis_and_a_falling_price_produce_a_divergence(self):
        self.thesis()
        self.window("90")
        summary = self.run_child()
        self.assertEqual(summary["events_by_kind"].get("price_divergence"), 1)
        event = ResearchEventAuthority(self.store).events(
            company_ref=ACN, kind="price_divergence")[0]
        self.assertEqual(event["payload"]["thesis_stance"], "long")
        self.assertEqual(event["payload"]["divergence_percent"], "10.0000")
        self.assertEqual(event["payload"]["window_days"], 10)

    def test_being_right_is_not_an_event(self):
        self.thesis()
        self.window("112")
        summary = self.run_child()
        self.assertIsNone(summary["events_by_kind"].get("price_divergence"))

    def test_a_company_with_no_thesis_diverges_from_nothing(self):
        self.window("90")
        summary = self.run_child()
        self.assertIsNone(summary["events_by_kind"].get("price_divergence"))

    def test_a_persisting_divergence_does_not_re_fire_the_next_day(self):
        self.thesis()
        self.window("90")
        self.run_child()
        again = self.run_child()
        self.assertIsNone(again["events_by_kind"].get("price_divergence"))
        self.assertEqual(
            len(ResearchEventAuthority(self.store).events(
                company_ref=ACN, kind="price_divergence")),
            1,
        )

    def test_the_stance_comes_from_the_policy_and_defaults_to_long(self):
        from dalton_core.tracking_cadence import load_policy
        from dalton_core.tracking_lane_cli import thesis_stances

        self.thesis()
        stances = thesis_stances(self.store, load_policy(POLICY_PATH), tracked=[ACN])
        self.assertEqual(stances[ACN]["stance"], "long")
        self.assertTrue(stances[ACN]["thesis_ref"].startswith("thesis-version:"))


class FairnessTests(P14aHarness):
    """B1: a cap on candidates in universe order starves every company but the first."""

    def setUp(self):
        super().setUp()
        for ref in (ACN, CTSH, EPAM):
            self.pass_screen(ref)

    def run_child(self, **kwargs):
        return run_tracking(
            state_dir=self.state_dir, summary_dir=self.state_dir / "summary",
            policy_path=POLICY_PATH, now=NOW, **kwargs,
        )

    def test_the_cap_counts_what_was_written_not_what_was_looked_at(self):
        for ref in (ACN, CTSH, EPAM):
            for index in range(4):
                self.claim(subject=ref, statement=f"{ref} fact {index}")
        first = self.run_child(max_events=6)
        self.assertEqual(first["events_recorded"], 6)
        events = ResearchEventAuthority(self.store)
        seen = {ref: len(events.events(company_ref=ref)) for ref in (ACN, CTSH, EPAM)}
        # Two apiece, not six for the first company.
        self.assertEqual(seen, {ACN: 2, CTSH: 2, EPAM: 2})

    def test_a_second_run_reaches_the_rest_rather_than_re_recognising_duplicates(self):
        for ref in (ACN, CTSH, EPAM):
            for index in range(4):
                self.claim(subject=ref, statement=f"{ref} fact {index}")
        self.run_child(max_events=6)
        second = self.run_child(max_events=6)
        self.assertEqual(second["events_recorded"], 6)
        events = ResearchEventAuthority(self.store)
        self.assertEqual(
            {ref: len(events.events(company_ref=ref)) for ref in (ACN, CTSH, EPAM)},
            {ACN: 4, CTSH: 4, EPAM: 4},
        )

    def test_a_company_with_far_more_candidates_does_not_crowd_the_others_out(self):
        for index in range(20):
            self.claim(subject=ACN, statement=f"ACN fact {index}")
        self.claim(subject=CTSH, statement="CTSH fact")
        self.claim(subject=EPAM, statement="EPAM fact")
        self.run_child(max_events=4)
        events = ResearchEventAuthority(self.store)
        self.assertEqual(len(events.events(company_ref=CTSH)), 1)
        self.assertEqual(len(events.events(company_ref=EPAM)), 1)
        self.assertEqual(len(events.events(company_ref=ACN)), 2)

    def test_the_interleave_is_one_per_company_in_universe_order(self):
        queues = {ACN: [{"i": 1}, {"i": 2}, {"i": 3}], CTSH: [{"i": 9}], EPAM: []}
        self.assertEqual(
            [row["i"] for row in round_robin(queues, order=[ACN, CTSH, EPAM])],
            [1, 9, 2, 3],
        )
