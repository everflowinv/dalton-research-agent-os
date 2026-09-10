"""C1: one company's diary a day, and the windows the tick reports opening.

No store, no subprocess and no real mission: the launcher and the authority are
fakes, and the mission is a plain dict. Publishing a real mission version to
test a lane would assert another pass's schedule, which is not this file's
business -- the same choice the price lane's tests made.
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from dalton_core.catalyst_calendar import UNCONFIRMED_DATE_CAVEAT
from dalton_core.lane_child_launcher import LaneChildConflict, LaneChildRejected
from tests.p14a_fixtures import AUTOMATION, P14aHarness
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
        # Mirrors the real reader's shape, caveat included: the lane consumes
        # the carried field rather than deriving it, so a fake that omitted it
        # would be testing a reader that does not exist.
        found = []
        for version in self.versions.values():
            for entry in version["entries"]:
                unconfirmed = entry["confidence"] != "confirmed"
                found.append({
                    **entry, "company_ref": version["company_ref"],
                    "date_unconfirmed": unconfirmed,
                    "date_caveat": UNCONFIRMED_DATE_CAVEAT if unconfirmed else "",
                    "days_until": 22,
                })
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
            "subject": "2026q4", "anchor_date": day, "expected_date": day,
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

    def test_without_a_writer_the_tick_says_so_and_records_nothing(self):
        _, settled = self.settle_with(authority=FakeAuthority({ACN: version(ACN)}))
        self.assertEqual(settled["events"]["status"], "events_unwired")
        self.assertEqual(settled["events"]["emitted"], [])
        self.assertIn("no window", settled["events"]["reason"])

    def test_an_estimated_date_opens_a_labelled_preview_from_the_lane_too(self):
        written = []
        _, settled = self.settle_with(
            authority=FakeAuthority({ACN: version(ACN, confidence="estimated")}),
            record_event=lambda **event: written.append(event),
        )
        emitted = settled["events"]["emitted"]
        self.assertEqual([item["window"] for item in emitted], ["preview"])
        self.assertEqual(emitted[0]["date_confidence"], "estimated")
        self.assertEqual(written[0]["payload"]["date_confidence"], "estimated")
        self.assertIs(written[0]["payload"]["confirmed"], False)

    def test_the_tick_strip_carries_the_caveat_an_operator_needs_to_see(self):
        coordinator = self.coordinator(
            authority=FakeAuthority({ACN: version(ACN, confidence="estimated")}),
            mission_value=ONE_COMPANY)
        launched = coordinator.dispatch_once()
        self.launcher.finish(launched["ticket_ref"], calendar_status="fresh")
        coordinator.dispatch_once()
        idle = coordinator.dispatch_once()
        self.assertEqual(idle["status"], "idle")
        self.assertEqual(idle["upcoming"][0]["date_confidence"], "estimated")
        self.assertEqual(idle["upcoming"][0]["date_caveat"], UNCONFIRMED_DATE_CAVEAT)

    def test_a_window_that_stays_open_is_asked_for_daily_and_stored_once(self):
        # De-duplication is the ledger's, not this process's: the payload
        # carries nothing that changes with the day, so the second morning
        # comes back as a duplicate. A set held here forgot everything on
        # restart and re-recorded every open window after a deploy.
        ledger = {}

        def record(**event):
            key = (event["company_ref"], tuple(sorted(event["payload"].items())))
            status = "duplicate" if key in ledger else "fresh"
            ledger[key] = 1
            return {"status": status, "id": "research-event:one"}

        coordinator, first = self.settle_with(
            authority=FakeAuthority({ACN: version(ACN)}), record_event=record)
        self.now = START + timedelta(days=1)
        launched = coordinator.dispatch_once()
        self.assertEqual(launched["status"], "launched")
        self.launcher.finish(launched["ticket_ref"], calendar_status="duplicate")
        second = coordinator.dispatch_once()["settled"]
        self.assertEqual(first["events"]["fresh_count"], 1)
        self.assertEqual(second["events"]["fresh_count"], 0)
        self.assertEqual(second["events"]["duplicate_count"], 1)
        self.assertEqual(len(ledger), 1)

    def test_a_writer_that_fails_halfway_does_not_lose_what_it_wrote(self):
        # Two windows open at once -- a preview and a date change -- and the
        # second write raises. Marking the batch afterwards meant the first was
        # recorded and forgotten, so the next tick wrote it a second time.
        written = []

        def flaky(**event):
            if len(written) >= 1:
                raise RuntimeError("the event ledger refused")
            written.append(event)

        moved = "catalyst-entry:one"
        coordinator = self.coordinator(
            authority=FakeAuthority({ACN: version(ACN)}),
            record_event=flaky, mission_value=ONE_COMPANY)
        launched = coordinator.dispatch_once()
        self.launcher.finish(launched["ticket_ref"], calendar_status="fresh",
                             moved_entry_refs=[moved])
        first = coordinator.dispatch_once()["settled"]
        self.assertEqual(first["events"]["status"], "failed")
        self.assertEqual(first["events"]["recorded_count"], 1)

        # Next day: the one that was written is not written again.
        self.now = START + timedelta(days=1)
        launched = coordinator.dispatch_once()
        self.launcher.finish(launched["ticket_ref"], calendar_status="fresh",
                             moved_entry_refs=[moved])
        coordinator.dispatch_once()
        self.assertEqual(len(written), 1)

    def test_a_partial_run_still_emits_and_still_charges_the_budget(self):
        # The filed half published, the vendor half did not. The day counts as
        # asked -- the calendar learned something -- and the failure counts, so
        # a vendor that stays broken cannot hide behind it.
        written = []
        coordinator = self.coordinator(
            authority=FakeAuthority({ACN: version(ACN)}),
            record_event=lambda **event: written.append(event),
            mission_value=ONE_COMPANY)
        launched = coordinator.dispatch_once()
        self.launcher.finish(launched["ticket_ref"], status="partial",
                             calendar_status="fresh", vendor_status="failed",
                             failure_reason="Yahoo returned nothing usable")
        settled = coordinator.dispatch_once()["settled"]
        self.assertEqual(settled["status"], "partial")
        self.assertEqual(settled["events"]["status"], "recorded")
        self.assertEqual(len(written), 1)
        self.assertFalse(coordinator.due(ACN))
        self.assertEqual(coordinator._failures[ACN], 1)

    def test_three_partial_days_hold_the_company_with_the_vendor_s_reason(self):
        coordinator = self.coordinator(
            authority=FakeAuthority({ACN: version(ACN)}),
            mission_value=ONE_COMPANY)
        for day in range(MAX_FAILURES_PER_COMPANY):
            self.now = START + timedelta(days=day)
            launched = coordinator.dispatch_once()
            self.assertEqual(launched["company_ref"], ACN)
            self.launcher.finish(launched["ticket_ref"], status="partial",
                                 calendar_status="duplicate",
                                 vendor_status="failed",
                                 failure_reason="Yahoo returned nothing usable")
            coordinator.dispatch_once()
        self.now = START + timedelta(days=MAX_FAILURES_PER_COMPANY)
        after = coordinator.dispatch_once()
        self.assertEqual(after["status"], "idle")
        held = [row for row in after["skipped"] if row["company_ref"] == ACN]
        self.assertEqual(held[0]["reason"], "held")
        self.assertIn("Yahoo returned nothing usable", held[0]["detail"])

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
        # Ordering, not adjacency: the daily-tracking lane (P14a) now sits
        # between the prices and the calendar, and adjacency was never the
        # decision -- prices before the calendar, calendar before the spec.
        self.assertLess(order.index("dispatch_mission_market_prices"),
                        order.index("dispatch_mission_catalyst_calendar"))
        self.assertLess(order.index("dispatch_mission_catalyst_calendar"),
                        order.index("dispatch_company_model_spec"))

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


class _CalendarWiring:
    """Setup shared by the two wiring cases. Not a TestCase: subclassing one
    would re-run every case against the other's grants."""

    def build(self):
        from dalton_core.catalyst_calendar import CatalystCalendarAuthority
        from dalton_core.research_event import ResearchEventAuthority

        self.calendar = CatalystCalendarAuthority(self.store)
        self.events = ResearchEventAuthority(self.store)
        self.launcher = FakeLauncher()
        self.now = START
        harness = self

        class Server:
            store = harness.store
            coverage_mission = harness.missions
            lane_state: dict = {}

            def lane_launcher(self, kwarg):
                return harness.launcher

        self.server = Server()

    def publish(self, day, *, confidence="estimated"):
        source = (
            {"kind": "filing", "ref": "sec:filing:0001467373-26-000031",
             "source_ref": "source:sec-edgar", "observed_date": day,
             "observed_at": f"{day}T12:00:00+00:00", "confidence": "confirmed",
             "note": ""}
            if confidence == "confirmed" else
            {"kind": "connector_invocation",
             "ref": "connector-invocation:yfinance:aaaa",
             "source_ref": "source:yahoo-finance", "observed_date": day,
             "observed_at": "2026-09-09T15:00:00+00:00",
             "confidence": "estimated", "note": ""}
        )
        return self.calendar.publish(
            company_ref=ACN,
            entries=[{"event_kind": "earnings", "sources": [source], "notes": ""}],
            change_reason="evidence_thicker", evidence_refs=[source["ref"]],
            now="2026-09-09",
        )

    def tick(self):
        """One dispatch, then take the clock off the coordinator it cached."""

        from dalton_core.mission_catalyst_lane import dispatch

        result = dispatch(self.server, {})
        coordinator = self.server.lane_state["catalyst_calendar_launcher"]
        coordinator.clock = lambda: self.now
        return result

    def rows(self):
        return self.store.connection.execute(
            "SELECT * FROM research_events WHERE kind='calendar'"
        ).fetchall()

    def run_a_full_cycle(self):
        """Tick until every company in the universe has been asked today.

        The mission carries five and the lane takes one a tick, so a test that
        only settled the first left a child open and the next day's tick came
        back ``busy``. Returns what the tick said about Accenture, which is the
        only one these tests give a calendar to.
        """

        found = None
        for _ in range(2 * len(self.mission["universe"]) + 2):
            result = self.tick()
            settled = result.get("settled")
            if settled and settled.get("company_ref") == ACN:
                found = {"settled": settled}
            if result["status"] == "launched":
                self.launcher.finish(result["ticket_ref"], calendar_status="fresh")
                continue
            if result["status"] in ("idle", "ungranted", "unconfigured"):
                break
        assert found is not None, "the tick never settled a child for ACN"
        return found


class WiringTests(P14aHarness, _CalendarWiring):
    """The bridge itself: a real store, a real ledger, and ``dispatch``.

    Every other test in this file uses a fake writer, which is what let the
    bridge be broken for its whole life without a red test: ``dispatch`` read
    ``record_research_event`` off the server, no writer has ever had that
    attribute, so the lane resolved ``None`` and reported ``events_unwired``
    every tick -- a fallback that looked exactly like wiring. So this one goes
    through ``dispatch`` and asserts a row in the table.
    """

    def setUp(self):
        super().setUp()
        self.build()

    def test_a_preview_window_puts_a_calendar_event_in_the_ledger(self):
        import json

        self.publish("2026-10-01")
        settled = self.run_a_full_cycle()["settled"]
        self.assertEqual(settled["events"]["status"], "recorded")
        self.assertEqual(settled["events"]["fresh_count"], 1)

        rows = self.rows()
        self.assertEqual(len(rows), 1)
        record = json.loads(rows[0]["record_json"])
        self.assertEqual(record["company_ref"], ACN)
        self.assertEqual(record["kind"], "calendar")
        self.assertEqual(record["evidence_tier"], "derived")
        self.assertEqual(record["actor_ref"], AUTOMATION)
        payload = record["payload"]
        self.assertEqual(payload["window"], "preview")
        self.assertEqual(payload["event_kind"], "earnings")
        self.assertEqual(payload["expected_date"], "2026-10-01")
        # The plan's requirement, on the row as stored rather than on the
        # dictionary the emitter built.
        self.assertEqual(payload["date_confidence"], "estimated")
        self.assertIs(payload["confirmed"], False)
        self.assertTrue(payload["calendar_version_ref"].startswith(
            "catalyst-calendar-version:"))

    def test_running_it_again_tomorrow_writes_no_second_row(self):
        self.publish("2026-10-01")
        self.run_a_full_cycle()
        self.now = START + timedelta(days=1)
        launched = self.tick()
        self.assertEqual(launched["status"], "launched")
        self.launcher.finish(launched["ticket_ref"], calendar_status="duplicate")
        settled = self.tick()["settled"]
        self.assertEqual(settled["events"]["duplicate_count"], 1)
        self.assertEqual(settled["events"]["fresh_count"], 0)
        self.assertEqual(len(self.rows()), 1)

    def test_the_confirmation_writes_a_second_row(self):
        import json

        self.publish("2026-10-01")
        self.run_a_full_cycle()
        # The company files its Item 2.02 for the same date.
        self.publish("2026-10-01", confidence="confirmed")
        self.now = START + timedelta(days=1)
        launched = self.tick()
        self.launcher.finish(launched["ticket_ref"], calendar_status="fresh")
        self.tick()
        rows = self.rows()
        self.assertEqual(len(rows), 2)
        self.assertEqual(
            sorted(json.loads(row["record_json"])["payload"]["date_confidence"]
                   for row in rows),
            ["confirmed", "estimated"],
        )

    def test_a_calibration_row_is_confirmed_and_says_so(self):
        import json

        self.publish("2026-09-09", confidence="confirmed")
        settled = self.run_a_full_cycle()["settled"]
        self.assertEqual(settled["events"]["fresh_count"], 1)
        payload = json.loads(self.rows()[0]["record_json"])["payload"]
        self.assertEqual(payload["window"], "calibration")
        self.assertEqual(payload["date_confidence"], "confirmed")
        self.assertIs(payload["confirmed"], True)


class UngrantedEventScopeTests(P14aHarness, _CalendarWiring):
    """A mission that may publish the calendar and may not record events.

    Which is exactly the state the live Core is in: it grants ``observation``
    and not ``market_event``. That is a permission the owner has not given yet,
    not a lane that is failing, and it must not spend the company's retry
    budget.
    """

    grants = ("observation", "stage_record")

    def setUp(self):
        super().setUp()
        self.build()

    def test_the_tick_says_ungranted_and_charges_nothing(self):
        self.publish("2026-10-01")
        settled = self.run_a_full_cycle()["settled"]
        self.assertEqual(settled["events"]["status"], "events_ungranted")
        self.assertIn("market_event", settled["events"]["reason"])
        self.assertEqual(self.rows(), [])
        coordinator = self.server.lane_state["catalyst_calendar_launcher"]
        self.assertEqual(coordinator._failures, {})
        # And the calendar itself is unaffected: the company was asked today.
        self.assertFalse(coordinator.due(ACN))
