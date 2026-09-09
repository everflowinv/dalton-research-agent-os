"""P11a: the price lane on a tick -- settle first, then start at most one child.

Queueless, like the specification lane: what needs fetching is derived from the
authority every tick, so there is nothing to leave stuck and a company that is
up to date simply is not chosen.

The grant is tested against a mission dict rather than a published mission
version on purpose. ``market_price`` is being added to
``coverage_mission.AUTOMATION_WRITE_SCOPES`` by the lane-registry pass running
in parallel with this one, so a test that published a real mission version here
would be asserting the other pass's schedule rather than this lane's logic.
What this lane owns is the decision -- grant present, dispatch; grant absent,
refuse and say why -- and that is what is under test.
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from dalton_core.lane_child_launcher import LaneChildConflict, LaneChildRejected
from dalton_core.mission_market_price_lane import (
    BACKFILL_YEARS,
    MAX_FAILURES_PER_COMPANY,
    SATISFIED_HOLD_SECONDS,
    WRITE_SCOPE,
    MissionMarketPriceLaneCoordinator,
    may_write_market_price,
)

ACN = "company:sec-cik:0001467373"
CTSH = "company:sec-cik:0001058290"
NOW = datetime(2026, 9, 9, 12, 0, tzinfo=timezone.utc)


def mission(*, may_write=(WRITE_SCOPE,), universe=None):
    return {
        "mission_ref": "coverage-mission:us-it-services",
        "universe": universe if universe is not None else [
            {"company_ref": ACN, "ticker": "ACN", "bootstrap_priority": "P0"},
            {"company_ref": CTSH, "ticker": "CTSH", "bootstrap_priority": "P1"},
        ],
        "autonomy": {
            "automation_principal": "automation:coverage-mission",
            "may_write": list(may_write),
        },
    }


def version(*, first="2023-09-09", last, provisional=False):
    """The shape of a stored version, as far as this lane reads it."""

    captured_at = (
        f"{last}T16:00:00+00:00" if provisional else f"{last}T23:30:00+00:00"
    )
    return {
        "first_bar_date": first,
        "last_bar_date": last,
        "bars": [{"date": last, "captured_at": captured_at}],
    }


class FakeAuthority:
    def __init__(self, latest=None):
        self.latest = dict(latest or {})

    def latest_version(self, company_ref):
        return self.latest.get(company_ref)


class FakeLauncher:
    def __init__(self, *, conflict=False, rejected=False):
        self.started = []
        self.tickets = {}
        self.conflict = conflict
        self.rejected = rejected
        self._n = 0

    def start(self, *, company_ref, ticker, start, end):
        if self.conflict:
            raise LaneChildConflict("market-price-run child is already running")
        if self.rejected:
            raise LaneChildRejected("a price run needs an approved record")
        self._n += 1
        ticket_id = f"market-price-run:{self._n:024d}"
        self.started.append({
            "company_ref": company_ref, "ticker": ticker,
            "start": start, "end": end, "ticket_ref": ticket_id,
        })
        self.tickets[ticket_id] = {
            "id": ticket_id, "company_ref": company_ref,
            "status": "running", "summary": None,
        }
        return dict(self.tickets[ticket_id])

    def finish(self, ticket_ref, *, status="succeeded", summary=None):
        self.tickets[ticket_ref] = {
            **self.tickets[ticket_ref], "status": status, "summary": summary,
        }

    def status(self, ticket_ref):
        return dict(self.tickets[ticket_ref])


class LaneTestCase(unittest.TestCase):
    def coordinator(self, *, authority=None, launcher=None, params=None,
                    clock=None):
        self.authority = authority or FakeAuthority()
        self.launcher = launcher or FakeLauncher()
        self.params = params if params is not None else mission()
        self.now = NOW
        return MissionMarketPriceLaneCoordinator(
            authority=self.authority, launcher=self.launcher,
            mission=lambda: self.params,
            clock=clock or (lambda: self.now),
        )


class GrantTests(LaneTestCase):
    def test_a_mission_without_the_grant_gets_no_child_and_a_reason(self):
        lane = self.coordinator(params=mission(may_write=("claim", "evidence")))
        result = lane.dispatch_once()
        self.assertEqual(result["status"], "ungranted")
        self.assertIn(WRITE_SCOPE, result["reason"])
        self.assertEqual(self.launcher.started, [])

    def test_the_grant_is_what_opens_the_lane(self):
        lane = self.coordinator()
        self.assertEqual(lane.dispatch_once()["status"], "launched")

    def test_no_mission_at_all_is_unconfigured_rather_than_ungranted(self):
        lane = self.coordinator(params=None)
        self.params = None
        self.assertEqual(lane.dispatch_once()["status"], "unconfigured")

    def test_the_grant_predicate_reads_the_mission_and_nothing_else(self):
        self.assertTrue(may_write_market_price(mission()))
        self.assertFalse(may_write_market_price(mission(may_write=())))
        self.assertFalse(may_write_market_price({}))
        self.assertFalse(may_write_market_price(None))
        # A string is a sequence; "market_price" must not satisfy the grant by
        # containing the word.
        self.assertFalse(may_write_market_price(
            {"autonomy": {"may_write": WRITE_SCOPE}}))


class WindowTests(LaneTestCase):
    def test_a_company_with_no_history_is_backfilled_three_years(self):
        lane = self.coordinator()
        start, end, kind = lane.window(ACN)
        self.assertEqual(start, "2023-09-09")
        # Yahoo's window excludes ``end``, so tomorrow is how today's bar gets
        # in.
        self.assertEqual(end, "2026-09-10")
        self.assertEqual(kind, "backfill")
        self.assertEqual(
            datetime.fromisoformat(start).year, NOW.year - BACKFILL_YEARS)

    def test_a_company_with_history_is_asked_only_for_what_it_is_missing(self):
        lane = self.coordinator(authority=FakeAuthority(
            {ACN: version(last="2026-09-04")}))
        self.assertEqual(
            lane.window(ACN), ("2026-09-05", "2026-09-10", "forward"))

    def test_a_company_already_current_has_no_window(self):
        lane = self.coordinator(authority=FakeAuthority(
            {ACN: version(last="2026-09-09")}))
        self.assertIsNone(lane.window(ACN))

    def test_a_provisional_last_bar_is_asked_for_again(self):
        # The blocker this rule exists for: a bar read at 16:00Z is the last
        # trade so far, not the close. Starting the window the day after would
        # freeze it forever and make the authority's restatement path
        # unreachable through the lane.
        lane = self.coordinator(authority=FakeAuthority(
            {ACN: version(last="2026-09-09", provisional=True)}))
        self.assertEqual(
            lane.window(ACN), ("2026-09-09", "2026-09-10", "restate_provisional"))

    def test_a_settled_last_bar_is_not_asked_for_again(self):
        lane = self.coordinator(authority=FakeAuthority(
            {ACN: version(last="2026-09-08")}))
        start, _end, kind = lane.window(ACN)
        self.assertEqual((start, kind), ("2026-09-09", "forward"))

    def test_a_series_short_at_the_far_end_asks_for_the_earlier_window(self):
        lane = self.coordinator(authority=FakeAuthority(
            {ACN: version(first="2025-01-06", last="2026-09-09")}))
        self.assertEqual(
            lane.window(ACN), ("2023-09-09", "2025-01-06", "backfill_gap"))

    def test_the_forward_gap_is_closed_before_the_backward_one(self):
        lane = self.coordinator(authority=FakeAuthority(
            {ACN: version(first="2025-01-06", last="2026-09-04")}))
        self.assertEqual(lane.window(ACN)[2], "forward")

    def test_a_backfill_that_found_nothing_is_not_asked_for_twice(self):
        # There is nothing behind a company's first trading day, and asking
        # again every six hours would never learn that.
        lane = self.coordinator(
            authority=FakeAuthority({ACN: version(first="2025-01-06",
                                                  last="2026-09-09")}),
            params=mission(universe=[
                {"company_ref": ACN, "ticker": "ACN", "bootstrap_priority": "P0"}]))
        launched = lane.dispatch_once()
        self.assertEqual(launched["window_kind"], "backfill_gap")
        self.launcher.finish(launched["ticket_ref"], summary={
            "series_status": "empty", "added_bar_count": 0})
        lane._settle_open()
        self.now = NOW + timedelta(seconds=SATISFIED_HOLD_SECONDS + 1)
        self.assertIsNone(lane.window(ACN))
        self.assertEqual(lane.dispatch_once()["status"], "idle")


class DispatchTests(LaneTestCase):
    def test_the_first_company_by_priority_goes_first(self):
        lane = self.coordinator()
        result = lane.dispatch_once()
        self.assertEqual(result["status"], "launched")
        self.assertEqual(result["company_ref"], ACN)
        self.assertEqual(result["ticker"], "ACN")

    def test_a_current_company_is_skipped_with_a_reason(self):
        lane = self.coordinator(authority=FakeAuthority(
            {ACN: version(last="2026-09-09")}))
        result = lane.dispatch_once()
        self.assertEqual(result["company_ref"], CTSH)
        self.assertEqual(
            [row["reason"] for row in result["skipped"]], ["current"])

    def test_everything_current_is_idle(self):
        lane = self.coordinator(authority=FakeAuthority({
            ACN: version(last="2026-09-09"),
            CTSH: version(last="2026-09-09"),
        }))
        result = lane.dispatch_once()
        self.assertEqual(result["status"], "idle")
        self.assertEqual(len(result["skipped"]), 2)

    def test_a_company_without_a_ticker_is_skipped_rather_than_guessed_at(self):
        lane = self.coordinator(params=mission(universe=[
            {"company_ref": ACN, "bootstrap_priority": "P0"},
            {"company_ref": CTSH, "ticker": "CTSH", "bootstrap_priority": "P1"},
        ]))
        self.assertEqual(lane.dispatch_once()["company_ref"], CTSH)

    def test_only_one_child_runs_at_a_time(self):
        lane = self.coordinator()
        lane.dispatch_once()
        second = lane.dispatch_once()
        self.assertEqual(second["status"], "busy")
        self.assertEqual(len(self.launcher.started), 1)

    def test_a_launcher_conflict_is_reported_not_raised(self):
        lane = self.coordinator(launcher=FakeLauncher(conflict=True))
        self.assertEqual(lane.dispatch_once()["status"], "busy")

    def test_a_rejected_launch_charges_the_company(self):
        lane = self.coordinator(launcher=FakeLauncher(rejected=True))
        result = lane.dispatch_once()
        self.assertEqual(result["status"], "rejected")
        self.assertIn("approved", result["reason"])


class SettleTests(LaneTestCase):
    def launch_and_finish(self, lane, **finish):
        result = lane.dispatch_once()
        self.launcher.finish(result["ticket_ref"], **finish)
        return result

    def test_a_child_is_settled_on_the_next_tick_not_the_one_that_spawned_it(self):
        # A child inspected in the same breath it was spawned is always still
        # running, and a lane that only ever looks at its own newborn never
        # learns anything.
        lane = self.coordinator()
        first = lane.dispatch_once()
        self.assertIsNone(first["settled"])
        self.launcher.finish(first["ticket_ref"], summary={
            "series_status": "fresh", "added_bar_count": 752,
            "last_bar_date": "2026-09-09",
        })
        # What the child published is now in the authority, which is where the
        # next tick looks -- the lane keeps no copy of it.
        self.authority.latest[ACN] = version(last="2026-09-09")
        second = lane.dispatch_once()
        self.assertEqual(second["settled"]["status"], "succeeded")
        self.assertEqual(second["settled"]["series_status"], "fresh")
        self.assertEqual(second["settled"]["company_ref"], ACN)
        # And the slot is free again, so the next company goes.
        self.assertEqual(second["company_ref"], CTSH)

    def test_a_running_child_holds_the_slot(self):
        lane = self.coordinator()
        lane.dispatch_once()
        second = lane.dispatch_once()
        self.assertEqual(second["settled"]["status"], "running")
        self.assertEqual(second["status"], "busy")

    def test_a_run_that_found_nothing_new_puts_the_company_aside_for_a_while(self):
        # A weekend tick would otherwise spend a call every five minutes
        # discovering that Saturday is still not a trading day.
        lane = self.coordinator()
        first = self.launch_and_finish(lane, summary={"series_status": "duplicate"})
        self.assertEqual(first["company_ref"], ACN)
        second = lane.dispatch_once()
        self.assertEqual(second["company_ref"], CTSH)
        self.launcher.finish(second["ticket_ref"], summary={"series_status": "duplicate"})
        third = lane.dispatch_once()
        self.assertEqual(third["status"], "idle")
        self.assertEqual(
            sorted(row["reason"] for row in third["skipped"]),
            ["recently_current", "recently_current"])

    def test_a_provisional_bar_is_restated_on_the_next_day_then_settles(self):
        # The blocker, end to end. Captured at 16:00Z on day D; the next tick
        # re-requests D, the child restates it and adds D+1, and the day after
        # the now-settled bar is not asked for again.
        lane = self.coordinator(
            authority=FakeAuthority(
                {ACN: version(last="2026-09-09", provisional=True)}),
            params=mission(universe=[
                {"company_ref": ACN, "ticker": "ACN", "bootstrap_priority": "P0"}]))
        first = lane.dispatch_once()
        self.assertEqual(first["requested_start"], "2026-09-09")
        self.assertEqual(first["window_kind"], "restate_provisional")
        self.launcher.finish(first["ticket_ref"], summary={
            "series_status": "fresh", "added_bar_count": 1,
            "restated_bar_dates": ["2026-09-09"],
            "provisional_bar_date": None, "last_bar_date": "2026-09-10",
        })
        self.authority.latest[ACN] = version(last="2026-09-10")
        self.now = NOW + timedelta(days=2)
        settled = lane.dispatch_once()
        self.assertEqual(settled["settled"]["restated_bar_dates"], ["2026-09-09"])
        # 2026-09-10 settled, so it is not asked for again: the window starts
        # the day after it rather than on it.
        self.assertEqual(settled["requested_start"], "2026-09-11")
        self.assertEqual(settled["window_kind"], "forward")

    def test_a_run_that_only_restated_still_puts_the_company_aside(self):
        # A restatement publishes a version, so it is not a duplicate -- but a
        # provisional bar always leaves a window to ask for, and without the
        # hold the lane would restate the same afternoon bar every tick until
        # the market closed.
        lane = self.coordinator(
            authority=FakeAuthority(
                {ACN: version(last="2026-09-09", provisional=True)}),
            params=mission(universe=[
                {"company_ref": ACN, "ticker": "ACN", "bootstrap_priority": "P0"}]))
        first = lane.dispatch_once()
        self.launcher.finish(first["ticket_ref"], summary={
            "series_status": "fresh", "added_bar_count": 0,
            "restated_bar_dates": ["2026-09-09"],
            "provisional_bar_date": "2026-09-09",
        })
        held = lane.dispatch_once()
        self.assertEqual(held["status"], "idle")
        self.assertEqual([row["reason"] for row in held["skipped"]],
                         ["recently_current"])

    def test_the_hold_expires(self):
        lane = self.coordinator(params=mission(universe=[
            {"company_ref": ACN, "ticker": "ACN", "bootstrap_priority": "P0"}]))
        self.launch_and_finish(lane, summary={"series_status": "duplicate"})
        held = lane.dispatch_once()
        self.assertEqual(held["status"], "idle")
        self.assertEqual([row["reason"] for row in held["skipped"]],
                         ["recently_current"])
        self.now = NOW + timedelta(seconds=SATISFIED_HOLD_SECONDS + 1)
        result = lane.dispatch_once()
        self.assertEqual(result["company_ref"], ACN)

    def test_a_company_whose_runs_keep_failing_stops_consuming_the_slot(self):
        lane = self.coordinator(params=mission(universe=[
            {"company_ref": ACN, "ticker": "ACN", "bootstrap_priority": "P0"}]))
        for _ in range(MAX_FAILURES_PER_COMPANY):
            result = lane.dispatch_once()
            self.assertEqual(result["status"], "launched")
            self.launcher.finish(result["ticket_ref"], status="failed", summary={
                "failure_reason": "MarketDataAdapterError: Yahoo has no such ticker",
            })
            lane._settle_open()
        held = lane.dispatch_once()
        self.assertEqual(held["status"], "idle")
        self.assertEqual([row["reason"] for row in held["skipped"]], ["held"])
        self.assertIn("no such ticker", held["skipped"][0]["detail"])

    def test_a_success_clears_the_failure_budget(self):
        lane = self.coordinator(params=mission(universe=[
            {"company_ref": ACN, "ticker": "ACN", "bootstrap_priority": "P0"}]))
        first = lane.dispatch_once()
        self.launcher.finish(first["ticket_ref"], status="failed", summary={
            "failure_reason": "boom"})
        second = lane.dispatch_once()
        self.launcher.finish(second["ticket_ref"], summary={
            "series_status": "fresh", "added_bar_count": 1})
        lane._settle_open()
        self.assertEqual(lane._failures, {})

    def test_an_orphaned_ticket_is_settled_rather_than_left_open(self):
        lane = self.coordinator()
        first = lane.dispatch_once()
        del self.launcher.tickets[first["ticket_ref"]]

        def status(ticket_ref):
            from dalton_core.lane_child_launcher import LaneChildTicketNotFound

            raise LaneChildTicketNotFound(ticket_ref)

        self.launcher.status = status
        second = lane.dispatch_once()
        self.assertEqual(second["settled"]["status"], "orphaned")
        self.assertEqual(second["status"], "launched")


class RegistrationTests(unittest.TestCase):
    """The lane declares itself once, and the rest is derived.

    P14-0 turned "adding a lane means editing ten regions of three modules"
    into one ``LaneSpec``. What is pinned here is this lane's half of that
    contract: the names it claims, that it turns itself off without an
    approval, and that importing it does not drag in the machinery that reads
    the registry.
    """

    def test_the_lane_is_registered_under_the_names_it_claims(self):
        from dalton_core.lane_registry import lane_for_operation
        from dalton_core.mission_market_price_lane import LANE, LAUNCHER_KWARG

        spec = lane_for_operation("dispatch_mission_market_prices")
        self.assertIs(spec, LANE)
        self.assertEqual(spec.driver_key, "mission_market_prices")
        self.assertEqual(spec.init_kwarg, LAUNCHER_KWARG)
        self.assertTrue(spec.core_discovery)
        # A tick takes no arguments.
        self.assertEqual(spec.param_fields, frozenset())

    def test_it_runs_between_the_statements_and_the_model_specification(self):
        from dalton_core.lane_registry import registered_lanes

        order = [spec.operation for spec in registered_lanes()]
        self.assertEqual(
            order[order.index("dispatch_mission_statements") + 1],
            "dispatch_mission_market_prices")
        self.assertEqual(
            order[order.index("dispatch_mission_market_prices") + 1],
            "dispatch_company_model_spec")

    def test_without_an_approval_there_is_no_launcher_and_no_argv(self):
        import argparse
        import tempfile
        from pathlib import Path

        from dalton_core.lane_registry import LaunchAgentContext
        from dalton_core.mission_market_price_lane import (
            MARKET_PRICE_GOVERNANCE, add_arguments, argv_fragment, build_launcher,
        )

        parser = argparse.ArgumentParser()
        parser.add_argument("--db")
        add_arguments(parser)
        args = parser.parse_args(["--db", "/tmp/core.sqlite"])
        self.assertIsNone(args.market_price_governance)
        self.assertIsNone(build_launcher(args))
        with tempfile.TemporaryDirectory() as directory:
            state = Path(directory)
            self.assertEqual(argv_fragment(LaunchAgentContext(state=state)), [])
            governance = state / "connector-governance" / MARKET_PRICE_GOVERNANCE
            governance.parent.mkdir(parents=True)
            governance.write_text("{}", encoding="utf-8")
            self.assertEqual(
                argv_fragment(LaunchAgentContext(state=state)),
                ["--market-price-governance", str(governance)])

    def test_a_writer_without_the_lane_says_so_rather_than_failing(self):
        from dalton_core.mission_market_price_lane import dispatch

        class Server:
            lane_state: dict = {}

            def lane_launcher(self, kwarg):
                return None

        result = dispatch(Server(), {})
        self.assertEqual(result["status"], "unconfigured")
        self.assertIn("market-price", result["reason"])

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
            import dalton_core.mission_market_price_lane  # noqa: F401
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
