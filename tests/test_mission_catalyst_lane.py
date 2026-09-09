"""C1: one company's diary a day, and the windows the tick reports opening.

No store, no subprocess and no real mission: the launcher and the authority are
fakes, and the mission is a plain dict. Publishing a real mission version to
test a lane would assert another pass's schedule, which is not this file's
business -- the same choice the price lane's tests made.
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from dalton_core.lane_child_launcher import LaneChildConflict, LaneChildRejected
from dalton_core.mission_catalyst_lane import (
    MAX_FAILURES_PER_COMPANY,
    WRITE_SCOPE,
    MissionCatalystLaneCoordinator,
    issuer_for,
    may_write_calendar,
)

ACN = "company:sec-cik:0001467373"
EPAM = "company:sec-cik:0001352010"
START = datetime(2026, 9, 9, 12, 0, tzinfo=timezone.utc)


def mission(*, may_write=(WRITE_SCOPE,), universe=None):
    return {
        "autonomy": {"may_write": list(may_write)},
        "universe": universe if universe is not None else [
            {"company_ref": ACN, "ticker": "ACN", "bootstrap_priority": "P0"},
            {"company_ref": EPAM, "ticker": "EPAM", "bootstrap_priority": "P1"},
        ],
    }


class FakeAuthority:
    def __init__(self, versions=None):
        self.versions = versions or {}

    def latest_version(self, company_ref):
        return self.versions.get(company_ref)

    def upcoming(self, now, horizon_days):
        found = []
        for version in self.versions.values():
            for entry in version["entries"]:
                found.append({**entry, "company_ref": version["company_ref"],
                              "days_until": 22})
        return found


class FakeLauncher:
    def __init__(self, *, conflict=False, rejected=False):
        self.conflict, self.rejected = conflict, rejected
        self.started = []
        self.tickets = {}

    def start(self, *, company_ref, ticker, as_of, issuer=None):
        if self.conflict:
            raise LaneChildConflict("a child is already running")
        if self.rejected:
            raise LaneChildRejected("no approved record")
        ticket_ref = f"catalyst-calendar-run:{len(self.started):024x}"
        self.started.append({"company_ref": company_ref, "ticker": ticker,
                             "issuer": issuer, "as_of": as_of})
        self.tickets[ticket_ref] = {
            "id": ticket_ref, "status": "running", "company_ref": company_ref,
            "ticker": ticker,
        }
        return self.tickets[ticket_ref]

    def finish(self, ticket_ref, *, status="succeeded", **summary):
        ticket = self.tickets[ticket_ref]
        ticket["status"] = status
        ticket["summary"] = {"failure_reason": None, **summary}

    def status(self, ticket_ref):
        return dict(self.tickets[ticket_ref])


def version(company_ref, *, day="2026-10-01", confidence="confirmed",
            entry_ref="catalyst-entry:one"):
    return {
        "id": "catalyst-calendar-version:one",
        "company_ref": company_ref,
        "entries": [{
            "entry_ref": entry_ref, "event_kind": "earnings",
            "subject": "2026q4", "expected_date": day,
            "confidence": confidence, "disagreement": False,
            "disagreeing_dates": [],
            "sources": [{"kind": "filing", "ref": "sec:filing:x",
                         "source_ref": "source:sec-edgar",
                         "observed_date": day,
                         "observed_at": "2026-09-01T00:00:00+00:00",
                         "confidence": confidence, "note": ""}],
            "notes": "",
        }],
    }


class LaneTestCase(unittest.TestCase):
    def coordinator(self, *, authority=None, launcher=None, record_event=None,
                    mission_value=None, now=START):
        self.now = now
        self.launcher = launcher or FakeLauncher()
        return MissionCatalystLaneCoordinator(
            authority=authority or FakeAuthority(),
            launcher=self.launcher,
            mission=lambda: mission() if mission_value is None else mission_value,
            record_event=record_event,
            clock=lambda: self.now,
        )


class GrantTests(unittest.TestCase):
    def test_the_scope_has_to_be_granted(self):
        self.assertTrue(may_write_calendar(mission()))
        self.assertFalse(may_write_calendar(mission(may_write=("claim",))))
        self.assertFalse(may_write_calendar(None))
        self.assertFalse(may_write_calendar({}))

    def test_a_bare_string_does_not_satisfy_the_grant_by_containing_the_word(self):
        self.assertFalse(may_write_calendar({"autonomy": {"may_write": WRITE_SCOPE}}))

    def test_the_scope_is_one_a_mission_may_actually_grant(self):
        from dalton_core.coverage_mission import AUTOMATION_WRITE_SCOPES

        self.assertIn(WRITE_SCOPE, AUTOMATION_WRITE_SCOPES)

    def test_an_ungranted_mission_starts_no_child_at_all(self):
        launcher = FakeLauncher()
        coordinator = MissionCatalystLaneCoordinator(
            authority=FakeAuthority(), launcher=launcher,
            mission=lambda: mission(may_write=("claim",)),
            clock=lambda: START,
        )
        result = coordinator.dispatch_once()
        self.assertEqual(result["status"], "ungranted")
        self.assertEqual(launcher.started, [])
        self.assertIn(WRITE_SCOPE, result["reason"])


class IssuerTests(unittest.TestCase):
    def test_the_company_ref_is_the_cik_mapping(self):
        self.assertEqual(issuer_for(ACN), "0001467373")
        # DXC's live ref is nine digits, which is a wart this must survive
        # rather than assume away.
        self.assertEqual(issuer_for("company:sec-cik:001688568"), "001688568")

    def test_a_company_that_is_not_an_sec_issuer_simply_has_no_cik(self):
        self.assertIsNone(issuer_for("company:some-other:acme"))
        self.assertIsNone(issuer_for("company:sec-cik:not-a-number"))
        self.assertIsNone(issuer_for(None))


class DispatchTests(LaneTestCase):
    def test_one_company_a_tick_in_the_mission_order(self):
        coordinator = self.coordinator()
        first = coordinator.dispatch_once()
        self.assertEqual(first["status"], "launched")
        self.assertEqual(first["company_ref"], ACN)
        self.assertEqual(first["issuer"], "0001467373")
        self.assertEqual(first["as_of"], "2026-09-09")

    def test_a_second_tick_while_a_child_runs_starts_nothing(self):
        coordinator = self.coordinator()
        coordinator.dispatch_once()
        second = coordinator.dispatch_once()
        self.assertEqual(second["status"], "busy")
        self.assertEqual(len(self.launcher.started), 1)

    def test_a_company_asked_today_is_not_asked_again_today(self):
        coordinator = self.coordinator()
        launched = coordinator.dispatch_once()
        self.launcher.finish(launched["ticket_ref"], calendar_status="fresh")
        second = coordinator.dispatch_once()
        self.assertEqual(second["company_ref"], EPAM)
        self.launcher.finish(second["ticket_ref"], calendar_status="duplicate")
        third = coordinator.dispatch_once()
        self.assertEqual(third["status"], "idle")
        self.assertEqual(
            {row["reason"] for row in third["skipped"]}, {"asked_today"})

    def test_tomorrow_it_is_asked_again(self):
        coordinator = self.coordinator()
        launched = coordinator.dispatch_once()
        self.launcher.finish(launched["ticket_ref"], calendar_status="fresh")
        coordinator.dispatch_once()
        self.now = START + timedelta(days=1)
        self.assertTrue(coordinator.due(ACN))

    def test_a_company_without_a_ticker_is_skipped_rather_than_guessed_at(self):
        coordinator = self.coordinator(mission_value=mission(universe=[
            {"company_ref": ACN, "ticker": "", "bootstrap_priority": "P0"},
        ]))
        self.assertEqual(coordinator.dispatch_once()["status"], "idle")
        self.assertEqual(self.launcher.started, [])

    def test_a_rejected_launch_charges_the_company_until_it_gives_up_the_slot(self):
        coordinator = self.coordinator(launcher=FakeLauncher(rejected=True))
        for _ in range(MAX_FAILURES_PER_COMPANY):
            self.assertEqual(coordinator.dispatch_once()["status"], "rejected")
        # ACN has spent its budget, so the tick moves on to the next company
        # rather than spending every tick on the one that cannot be served.
        after = coordinator.dispatch_once()
        self.assertEqual(after["company_ref"], EPAM)
        self.assertEqual(
            [row for row in after["skipped"] if row["company_ref"] == ACN][0]["reason"],
            "held",
        )

    def test_a_conflict_is_reported_as_busy_and_not_as_a_failure(self):
        coordinator = self.coordinator(launcher=FakeLauncher(conflict=True))
        self.assertEqual(coordinator.dispatch_once()["status"], "busy")


class SettleTests(LaneTestCase):
    def test_a_failed_run_does_not_count_as_having_asked_today(self):
        coordinator = self.coordinator()
        launched = coordinator.dispatch_once()
        self.launcher.finish(launched["ticket_ref"], status="failed",
                             failure_reason="Yahoo said no")
        coordinator.dispatch_once()
        # A transient error must not cost a day of the calendar.
        self.assertTrue(coordinator.due(ACN))

    def test_repeated_failures_release_the_slot(self):
        coordinator = self.coordinator()
        for _ in range(MAX_FAILURES_PER_COMPANY):
            launched = coordinator.dispatch_once()
            self.assertEqual(launched["company_ref"], ACN)
            self.launcher.finish(launched["ticket_ref"], status="failed",
                                 failure_reason="Yahoo said no")
        after = coordinator.dispatch_once()
        self.assertEqual(after["company_ref"], EPAM)
        self.assertEqual(
            [row for row in after["skipped"] if row["company_ref"] == ACN][0]["reason"],
            "held",
        )

    def test_a_child_that_died_before_writing_a_summary_is_still_attributable(self):
        coordinator = self.coordinator()
        launched = coordinator.dispatch_once()
        ticket = self.launcher.tickets[launched["ticket_ref"]]
        ticket["status"] = "orphaned"
        settled = coordinator.dispatch_once()["settled"]
        self.assertEqual(settled["company_ref"], ACN)
        self.assertEqual(settled["status"], "orphaned")


ONE_COMPANY = mission(universe=[
    {"company_ref": ACN, "ticker": "ACN", "bootstrap_priority": "P0"},
])


class EventTests(LaneTestCase):
    """One company, so that a tick is one child and the settle is unambiguous."""

    def settle_with(self, *, authority, record_event=None, moved=()):
        coordinator = self.coordinator(authority=authority,
                                       record_event=record_event,
                                       mission_value=ONE_COMPANY)
        launched = coordinator.dispatch_once()
        self.launcher.finish(launched["ticket_ref"], calendar_status="fresh",
                             moved_entry_refs=list(moved))
        return coordinator, coordinator.dispatch_once()["settled"]

    def test_a_confirmed_date_inside_the_preview_window_is_recorded(self):
        written = []
        _, settled = self.settle_with(
            authority=FakeAuthority({ACN: version(ACN)}),
            record_event=lambda **event: written.append(event),
        )
        self.assertEqual(settled["events"]["status"], "recorded")
        self.assertEqual([item["window"] for item in settled["events"]["emitted"]],
                         ["preview"])
        self.assertEqual(written[0]["kind"], "calendar")

    def test_without_a_writer_the_windows_are_reported_rather_than_lost(self):
        _, settled = self.settle_with(authority=FakeAuthority({ACN: version(ACN)}))
        self.assertEqual(settled["events"]["status"], "events_unwired")
        self.assertEqual(len(settled["events"]["emitted"]), 1)
        self.assertIn("nothing recorded them", settled["events"]["reason"])

    def test_an_estimated_date_opens_no_preview_from_the_lane_either(self):
        _, settled = self.settle_with(
            authority=FakeAuthority({ACN: version(ACN, confidence="estimated")}),
            record_event=lambda **event: None,
        )
        self.assertEqual(settled["events"]["emitted"], [])

    def test_a_window_that_stays_open_is_recorded_once_per_process(self):
        written = []
        coordinator, first = self.settle_with(
            authority=FakeAuthority({ACN: version(ACN)}),
            record_event=lambda **event: written.append(event),
        )
        # Same company, next day, same date: the window is still open and has
        # already been recorded.
        self.now = START + timedelta(days=1)
        launched = coordinator.dispatch_once()
        self.assertEqual(launched["status"], "launched")
        self.launcher.finish(launched["ticket_ref"], calendar_status="duplicate")
        second = coordinator.dispatch_once()["settled"]
        self.assertEqual(len(first["events"]["emitted"]), 1)
        self.assertEqual(second["events"]["emitted"], [])
        self.assertEqual(len(written), 1)

    def test_a_company_with_no_calendar_yet_emits_nothing_and_says_so(self):
        _, settled = self.settle_with(authority=FakeAuthority())
        self.assertEqual(settled["events"]["status"], "no_calendar")


class RegistrationTests(unittest.TestCase):
    """The lane declares itself once, and the rest is derived."""

    def test_the_lane_is_registered_under_the_names_it_claims(self):
        from dalton_core.lane_registry import lane_for_operation
        from dalton_core.mission_catalyst_lane import LANE, LAUNCHER_KWARG

        spec = lane_for_operation("dispatch_mission_catalyst_calendar")
        self.assertIs(spec, LANE)
        self.assertEqual(spec.driver_key, "mission_catalyst_calendar")
        self.assertEqual(spec.init_kwarg, LAUNCHER_KWARG)
        self.assertTrue(spec.core_discovery)
        self.assertEqual(spec.param_fields, frozenset())

    def test_it_runs_between_the_prices_and_the_model_specification(self):
        from dalton_core.lane_registry import registered_lanes

        order = [spec.operation for spec in registered_lanes()]
        self.assertEqual(
            order[order.index("dispatch_mission_market_prices") + 1],
            "dispatch_mission_catalyst_calendar")
        self.assertEqual(
            order[order.index("dispatch_mission_catalyst_calendar") + 1],
            "dispatch_company_model_spec")

    def test_the_operation_reaches_the_writer_and_the_driver(self):
        from dalton_core import writer_server
        from dalton_core.lane_registry import tick_lanes

        operation = "dispatch_mission_catalyst_calendar"
        self.assertIn(operation, writer_server.CORE_OPERATIONS)
        self.assertIn(operation, writer_server.CORE_DISCOVERY_OPERATIONS)
        self.assertIn(operation, writer_server.OPERATION_FIELDS)
        self.assertIn("mission_catalyst_calendar",
                      [spec.driver_key for spec in tick_lanes()])

    def test_without_an_approval_there_is_no_launcher_and_no_argv(self):
        import argparse
        import tempfile
        from pathlib import Path

        from dalton_core.lane_registry import LaunchAgentContext
        from dalton_core.mission_catalyst_lane import (
            CALENDAR_GOVERNANCE, add_arguments, argv_fragment, build_launcher,
        )

        parser = argparse.ArgumentParser()
        parser.add_argument("--db")
        add_arguments(parser)
        args = parser.parse_args(["--db", "/tmp/core.sqlite"])
        self.assertIsNone(args.catalyst_calendar_governance)
        self.assertIsNone(build_launcher(args))
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            self.assertEqual(argv_fragment(LaunchAgentContext(state=state)), [])
            governance = state / "connector-governance" / CALENDAR_GOVERNANCE
            governance.parent.mkdir(parents=True)
            governance.write_text("{}", encoding="utf-8")
            self.assertEqual(
                argv_fragment(LaunchAgentContext(state=state)),
                ["--catalyst-calendar-governance", str(governance)])

    def test_a_writer_without_the_lane_says_so_rather_than_failing(self):
        from dalton_core.mission_catalyst_lane import dispatch

        class Server:
            lane_state: dict = {}

            def lane_launcher(self, kwarg):
                return None

        result = dispatch(Server(), {})
        self.assertEqual(result["status"], "unconfigured")
        self.assertIn("catalyst-calendar", result["reason"])

    def test_importing_this_module_does_not_pull_in_the_writer(self):
        # The registry's own rule, checked here too because this module is the
        # one being added: a lane module that imports writer_server would have
        # writer_server fold in a half-built registry, and the lane would be
        # dispatched by the tick and refused by the writer for the life of the
        # process.
        import subprocess
        import sys
        import textwrap
        from pathlib import Path

        import dalton_core

        root = Path(dalton_core.__file__).resolve().parents[1]
        script = textwrap.dedent("""
            import sys
            import dalton_core.mission_catalyst_lane  # noqa: F401
            print(",".join(sorted(
                module for module in sys.modules
                if module in ("dalton_core.writer_server",
                              "dalton_core.bounded_planner_driver",
                              "dalton_core.macos_launchagent")
            )))
        """)
        finished = subprocess.run(
            [sys.executable, "-c", script], capture_output=True, text=True,
            env={"PYTHONPATH": str(root), "PATH": "/usr/bin:/bin"}, timeout=120,
        )
        self.assertEqual(finished.returncode, 0, finished.stderr)
        self.assertEqual(finished.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
