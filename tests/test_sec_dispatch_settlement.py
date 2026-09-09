"""P12b: a SEC dispatch that has run is over, and the quarter lane moves on.

Live, thirty-five dispatches sat 'launched' for a day across five companies.
Nothing ever moved a dispatch out of that state -- the status CHECK has no
terminal success value -- and the quarterly dispatcher refuses to queue while a
company has an open dispatch. So after the first batch every company looked
permanently busy and the financials froze one quarter back.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from dalton_core.coverage_mission import (
    CoverageMissionAuthority,
    CoverageMissionConflict,
    CoverageMissionNotFound,
    CoverageMissionValidationError,
)
from dalton_core.store import DaltonStore
from tests.p9a_fixtures import bootstrap_method_authorities, mission_params

ACN = "company:sec-cik:0001467373"


class SettlementTests(unittest.TestCase):
    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.store = DaltonStore(str(Path(self._dir.name) / "core.sqlite"))
        self.addCleanup(self.store.close)
        self.state = bootstrap_method_authorities(self.store)
        self.authority = CoverageMissionAuthority(self.store)
        params = mission_params(self.state)
        self.mission = self.authority.create_mission(params.pop("mission_ref"), **params)

    def dispatch(self, accession: str = "0001467373-25-000217") -> str:
        """One launched dispatch row, written the way the runner writes it."""

        dispatch_id = "mission-sec-dispatch:" + accession.replace("-", "")[:24]
        with self.authority._transaction() as cur:
            cur.execute(
                "INSERT INTO coverage_mission_sec_dispatches("
                "dispatch_id,mission_version_ref,mission_version_hash,company_ref,ticker,"
                "actor_ref,form,filed_from,filed_to,expected_accession,observation_ref,"
                "authorization_json,request_hash,status,ticket_ref,created_at,updated_at) "
                "VALUES(?,?,?,?,?,?,'10-Q','2025-07-01','2025-07-05',?,'obs','{}','h',"
                "'launched','sec-lane:aaaa','2026-09-07T18:00:00+00:00','2026-09-07T18:00:00+00:00')",
                (dispatch_id, self.mission["id"], self.mission["content_hash"], ACN, "ACN",
                 "automation:coverage-mission", accession),
            )
        return dispatch_id

    def test_a_launched_dispatch_is_open_until_it_is_settled(self):
        dispatch_id = self.dispatch()
        self.assertEqual([d["dispatch_id"] for d in self.authority.unsettled_sec_dispatches()],
                         [dispatch_id])
        self.authority.settle_sec_dispatch(dispatch_id, outcome="finished",
                                           ticket_ref="sec-lane:aaaa", detail="succeeded")
        self.assertEqual(self.authority.unsettled_sec_dispatches(), [])

    def test_a_ticket_that_is_gone_settles_as_orphaned_rather_than_forever_open(self):
        # Treating "gone" as "still running" is what stuck the lane for a day.
        dispatch_id = self.dispatch()
        settled = self.authority.settle_sec_dispatch(dispatch_id, outcome="orphaned")
        self.assertEqual(settled["outcome"], "orphaned")
        self.assertEqual(self.authority.unsettled_sec_dispatches(), [])

    def test_settling_twice_records_once(self):
        dispatch_id = self.dispatch()
        first = self.authority.settle_sec_dispatch(dispatch_id, outcome="finished")
        again = self.authority.settle_sec_dispatch(dispatch_id, outcome="finished")
        self.assertEqual(first["status_marker"], "fresh")
        self.assertEqual(again["status_marker"], "duplicate")

    def test_an_unknown_outcome_or_dispatch_is_refused(self):
        dispatch_id = self.dispatch()
        with self.assertRaises(CoverageMissionValidationError):
            self.authority.settle_sec_dispatch(dispatch_id, outcome="probably-fine")
        with self.assertRaises(CoverageMissionNotFound):
            self.authority.settle_sec_dispatch("mission-sec-dispatch:nope", outcome="finished")

    def test_a_pending_dispatch_has_not_run_and_cannot_settle(self):
        dispatch_id = self.dispatch()
        with self.authority._transaction() as cur:
            cur.execute("UPDATE coverage_mission_sec_dispatches SET status='pending' "
                        "WHERE dispatch_id=?", (dispatch_id,))
        with self.assertRaises(CoverageMissionConflict):
            self.authority.settle_sec_dispatch(dispatch_id, outcome="finished")

    def test_settlements_are_append_only_and_authority_only(self):
        dispatch_id = self.dispatch()
        self.authority.settle_sec_dispatch(dispatch_id, outcome="finished")
        for statement in (
            "UPDATE coverage_mission_sec_dispatch_settlements SET outcome='orphaned'",
            "DELETE FROM coverage_mission_sec_dispatch_settlements",
            "INSERT INTO coverage_mission_sec_dispatch_settlements("
            "dispatch_id,ticket_ref,outcome,detail,settled_at) VALUES('x',NULL,'finished',NULL,'t')",
        ):
            with self.assertRaises(Exception):
                self.store.connection.execute(statement)


class OpenCountTests(SettlementTests):
    """The count the quarterly dispatcher actually gates on."""

    def open_count(self) -> int:
        from dalton_core.mission_sec_quarters import MissionSecQuartersCoordinator

        return MissionSecQuartersCoordinator._open_dispatches(
            type("S", (), {"connection": self.store.connection})(), ACN)

    def test_a_settled_dispatch_stops_blocking_the_next_quarter(self):
        dispatch_id = self.dispatch()
        self.assertEqual(self.open_count(), 1)
        self.authority.settle_sec_dispatch(dispatch_id, outcome="finished")
        self.assertEqual(self.open_count(), 0)

    def test_an_unsettled_dispatch_still_blocks(self):
        # The original rule is worth keeping: queueing faster than the lane can
        # run just grows a backlog.
        self.dispatch()
        self.assertEqual(self.open_count(), 1)


class OutcomeTests(unittest.TestCase):
    """P13z: "the run is over" is not "the run worked".

    Settling on termination is what unblocked the quarter lane, and it was
    right. But nothing then asked whether any of it had succeeded: live, 73
    dispatches settled ``finished`` while every run had failed on the same
    connector conflict, and the only trace was a ``detail`` column no read
    returned. Five companies went a day with no filing arriving and the ledger
    looked healthy.
    """

    def setUp(self) -> None:
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.store = DaltonStore(str(Path(self._dir.name) / "core.sqlite"))
        self.addCleanup(self.store.close)
        self.state = bootstrap_method_authorities(self.store)
        self.authority = CoverageMissionAuthority(self.store)
        params = mission_params(self.state)
        self.mission = self.authority.create_mission(params.pop("mission_ref"), **params)

    def settle(self, accession: str, detail: str) -> None:
        dispatch_id = SettlementTests.dispatch(self, accession)
        self.authority.settle_sec_dispatch(
            dispatch_id, outcome="finished", ticket_ref="sec-lane:t", detail=detail)

    def test_runs_that_finished_without_succeeding_are_counted(self):
        self.settle("0001467373-25-000217", "failed")
        self.settle("0001467373-25-000218", "orphaned")
        self.settle("0001467373-25-000219", "succeeded")
        outcomes = self.authority.sec_dispatch_outcomes(ACN)
        self.assertEqual(outcomes["settled"], 3)
        self.assertEqual(outcomes["succeeded"], 1)
        self.assertEqual(outcomes["unsuccessful"], 2)

    def test_the_breakdown_survives_rather_than_collapsing_to_pass_fail(self):
        # A pruned ticket is a different problem from a run that failed;
        # counting them as one hides whichever is rarer.
        self.settle("0001467373-25-000217", "failed")
        self.settle("0001467373-25-000218", "orphaned")
        self.assertEqual(self.authority.sec_dispatch_outcomes(ACN)["by_detail"],
                         {"failed": 1, "orphaned": 1})

    def test_the_most_recent_failure_is_named(self):
        self.settle("0001467373-25-000217", "failed")
        outcomes = self.authority.sec_dispatch_outcomes(ACN)
        self.assertEqual(outcomes["last_failure_detail"], "failed")
        self.assertIsNotNone(outcomes["last_failure_at"])

    def test_a_company_with_only_successes_reports_no_failure(self):
        self.settle("0001467373-25-000217", "succeeded")
        outcomes = self.authority.sec_dispatch_outcomes(ACN)
        self.assertEqual((outcomes["unsuccessful"], outcomes["last_failure_detail"]), (0, None))

    def test_an_unknown_terminal_status_is_not_assumed_benign(self):
        # A status nobody has seen before is a run that produced nothing until
        # somebody says otherwise.
        self.settle("0001467373-25-000217", "cancelled")
        self.assertEqual(self.authority.sec_dispatch_outcomes(ACN)["unsuccessful"], 1)

    def test_outcomes_are_scoped_to_the_company_asked_about(self):
        self.settle("0001467373-25-000217", "failed")
        self.assertEqual(
            self.authority.sec_dispatch_outcomes("company:sec-cik:0000051143")["settled"], 0)
        self.assertEqual(self.authority.sec_dispatch_outcomes()["settled"], 1)


if __name__ == "__main__":
    unittest.main()
