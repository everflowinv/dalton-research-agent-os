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
        start, end = lane.window(ACN)
        self.assertEqual(start, "2023-09-09")
        # Yahoo's window excludes ``end``, so tomorrow is how today's bar gets
        # in.
        self.assertEqual(end, "2026-09-10")
        self.assertEqual(
            datetime.fromisoformat(start).year, NOW.year - BACKFILL_YEARS)

    def test_a_company_with_history_is_asked_only_for_what_it_is_missing(self):
        lane = self.coordinator(authority=FakeAuthority(
            {ACN: {"last_bar_date": "2026-09-04"}}))
        self.assertEqual(lane.window(ACN), ("2026-09-05", "2026-09-10"))

    def test_a_company_already_current_has_no_window(self):
        lane = self.coordinator(authority=FakeAuthority(
            {ACN: {"last_bar_date": "2026-09-09"}}))
        self.assertIsNone(lane.window(ACN))


class DispatchTests(LaneTestCase):
    def test_the_first_company_by_priority_goes_first(self):
        lane = self.coordinator()
        result = lane.dispatch_once()
        self.assertEqual(result["status"], "launched")
        self.assertEqual(result["company_ref"], ACN)
        self.assertEqual(result["ticker"], "ACN")

    def test_a_current_company_is_skipped_with_a_reason(self):
        lane = self.coordinator(authority=FakeAuthority(
            {ACN: {"last_bar_date": "2026-09-09"}}))
        result = lane.dispatch_once()
        self.assertEqual(result["company_ref"], CTSH)
        self.assertEqual(
            [row["reason"] for row in result["skipped"]], ["current"])

    def test_everything_current_is_idle(self):
        lane = self.coordinator(authority=FakeAuthority({
            ACN: {"last_bar_date": "2026-09-09"},
            CTSH: {"last_bar_date": "2026-09-09"},
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
        self.authority.latest[ACN] = {"last_bar_date": "2026-09-09"}
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


if __name__ == "__main__":
    unittest.main()
