"""P13ak: the statements lane on a tick -- queue, launch, settle, record.

The connector worked by hand before this existed. What these tests hold to
account is the part that runs unattended: that a finished child's numbers reach
the ledger, that a company whose runs keep failing stops consuming the single
slot, and that a rejection is not permanent.
"""

from __future__ import annotations

import unittest

from dalton_core.coverage_mission import CoverageMissionAuthority
from dalton_core.lane_child_launcher import (
    LaneChildConflict,
    LaneChildRejected,
    LaneChildTicketNotFound,
)
from dalton_core.mission_statement_lane import (
    MAX_FAILURES_PER_COMPANY,
    MissionStatementLaneCoordinator,
)
from dalton_core.store import DaltonStore
from tests.p9a_fixtures import bootstrap_method_authorities, mission_params

ACN = "company:sec-cik:0001467373"
ACCESSION = "0001467373-26-000031"


def _observation(accession=ACCESSION):
    return {
        "schema_version": "0.1", "cik": "0001467373", "entity_name": "Accenture plc",
        "filings": [{
            "accession": accession, "form": "10-Q", "filed": "2026-06-25",
            "report_date": "2026-06-30",
            "lines": [{
                "statement": "income", "concept": "us-gaap:Revenues",
                "label": "Revenues", "level": 0, "parent_concept": None,
                "is_breakdown": False, "dimension_axis": None,
                "dimension_member": None, "period_start": "2026-04-01",
                "period_end": "2026-06-30", "value": "17700000000",
                "unit": "USD", "balance": "credit",
            }],
        }],
        "source_record_refs": ["raw-sink:" + "c" * 64],
        "next_cursor": None, "provider_status": 200,
    }


class FakeLauncher:
    """A launcher that hands back whatever the test says the child did."""

    def __init__(self):
        self.tickets: dict[str, dict] = {}
        self.started: list[dict] = []
        self.next_ticket = None
        self.raise_on_start: Exception | None = None
        self.raise_on_status: Exception | None = None

    def start(self, *, ticker, form, limit, actor_ref):
        if self.raise_on_start is not None:
            raise self.raise_on_start
        self.started.append({"ticker": ticker, "form": form, "limit": limit,
                             "actor_ref": actor_ref})
        ticket_id = f"sec-financials-run:{len(self.started):024d}"
        self.tickets[ticket_id] = self.next_ticket or {
            "id": ticket_id, "status": "running", "summary": None}
        self.tickets[ticket_id]["id"] = ticket_id
        return {"id": ticket_id}

    def finish(self, ticket_id, *, status="succeeded", summary=None):
        self.tickets[ticket_id] = {"id": ticket_id, "status": status,
                                   "summary": summary}

    def status(self, ticket_ref):
        if self.raise_on_status is not None:
            raise self.raise_on_status
        if ticket_ref not in self.tickets:
            raise LaneChildTicketNotFound(ticket_ref)
        return self.tickets[ticket_ref]


class StatementLaneTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = DaltonStore(":memory:")
        self.addCleanup(self.store.close)
        self.state = bootstrap_method_authorities(self.store)
        self.missions = CoverageMissionAuthority(self.store)
        params = mission_params(self.state)
        self.mission = self.missions.create_mission(params.pop("mission_ref"), **params)
        self.launcher = FakeLauncher()
        self.companies = [{"company_ref": ACN, "ticker": "ACN"}]
        self.lane = MissionStatementLaneCoordinator(
            missions=self.missions, launcher=self.launcher,
            checklist=lambda: self.companies,
        )

    def succeeded_summary(self, observation=None):
        return {
            "status": "succeeded",
            "governance_ref": "connector-governance:sec-financial-statements:v2",
            "governance_hash": "b" * 64,
            "observation": observation or _observation(),
        }

    def test_a_tick_queues_and_launches_one_company(self):
        result = self.lane.dispatch_once()
        self.assertEqual(result["status"], "launched")
        self.assertEqual(result["company_ref"], ACN)
        self.assertEqual(self.launcher.started[0]["ticker"], "ACN")
        self.assertEqual(self.launcher.started[0]["form"], "10-Q")

    def test_a_company_in_flight_is_not_queued_again(self):
        self.lane.dispatch_once()
        second = self.lane.dispatch_once()
        self.assertEqual(second["queued"], [])
        self.assertEqual(second["status"], "idle")
        self.assertEqual(len(self.launcher.started), 1)

    def test_a_finished_run_puts_its_numbers_in_the_ledger(self):
        launched = self.lane.dispatch_once()
        self.launcher.finish(launched["ticket_ref"], summary=self.succeeded_summary())
        settled = self.lane.dispatch_once()
        self.assertEqual(settled["settled"][0]["outcome"], "succeeded")
        self.assertEqual(settled["settled"][0]["line_count"], 1)
        held = self.missions.statement_filings(ACN)
        self.assertEqual([item["accession"] for item in held], [ACCESSION])
        lines = self.missions.statement_lines(held[0]["ingest_id"])
        self.assertEqual(lines[0]["value"], "17700000000")

    def test_a_company_already_covered_is_not_queued_again(self):
        launched = self.lane.dispatch_once()
        self.launcher.finish(launched["ticket_ref"], summary=self.succeeded_summary())
        self.lane.dispatch_once()
        quiet = self.lane.dispatch_once()
        self.assertEqual(quiet["queued"], [])
        self.assertEqual(quiet["status"], "idle")

    def test_a_failed_run_settles_with_the_reason_the_child_gave(self):
        launched = self.lane.dispatch_once()
        self.launcher.finish(
            launched["ticket_ref"], status="failed",
            summary={"status": "failed",
                     "failure_reason": "SecFinancialsRunError: no XBRL"})
        settled = self.lane.dispatch_once()
        self.assertEqual(settled["settled"][0]["outcome"], "failed")
        self.assertIn("no XBRL", settled["settled"][0]["failure_reason"])

    def test_an_orphaned_ticket_settles_rather_than_hanging(self):
        launched = self.lane.dispatch_once()
        self.launcher.finish(launched["ticket_ref"], status="orphaned")
        settled = self.lane.dispatch_once()
        self.assertEqual(settled["settled"][0]["outcome"], "failed")
        self.assertIn("orphaned", settled["settled"][0]["failure_reason"])

    def test_a_success_with_no_observation_is_a_failure(self):
        launched = self.lane.dispatch_once()
        self.launcher.finish(launched["ticket_ref"],
                             summary={"status": "succeeded", "observation": None})
        settled = self.lane.dispatch_once()
        self.assertEqual(settled["settled"][0]["outcome"], "failed")
        self.assertEqual(self.missions.statement_filings(ACN), [])

    def test_a_running_child_is_left_alone(self):
        self.lane.dispatch_once()
        settled = self.lane.dispatch_once()
        self.assertEqual(settled["settled"], [])
        self.assertEqual(
            len(self.missions.launched_statement_dispatches()), 1)

    def test_an_unreadable_ticket_is_retried_not_settled(self):
        self.lane.dispatch_once()
        self.launcher.raise_on_status = OSError("disk hiccup")
        settled = self.lane.dispatch_once()
        self.assertEqual(settled["settled"], [])
        self.assertEqual(len(self.missions.launched_statement_dispatches()), 1)

    def test_a_rejected_launch_can_be_retried_once_governance_lands(self):
        self.launcher.raise_on_start = LaneChildRejected("governance is not approved")
        first = self.lane.dispatch_once()
        self.assertEqual(first["status"], "rejected")
        self.launcher.raise_on_start = None
        # A retry is a distinct dispatch, so the earlier rejection does not
        # freeze this company out forever.
        second = self.lane.dispatch_once()
        self.assertEqual(second["status"], "launched")

    def test_a_company_that_keeps_failing_stops_consuming_the_slot(self):
        for _ in range(MAX_FAILURES_PER_COMPANY):
            launched = self.lane.dispatch_once()
            self.assertEqual(launched["status"], "launched")
            self.launcher.finish(launched["ticket_ref"], status="failed",
                                 summary={"failure_reason": "parser broke"})
        exhausted = self.lane.dispatch_once()
        self.assertEqual(exhausted["queued"], [])
        self.assertEqual(exhausted["status"], "idle")
        self.assertEqual(len(self.launcher.started), MAX_FAILURES_PER_COMPANY)

    def test_a_busy_launcher_defers_rather_than_losing_the_dispatch(self):
        self.launcher.raise_on_start = LaneChildConflict("a child is already running")
        result = self.lane.dispatch_once()
        self.assertEqual(result["status"], "deferred")
        self.assertEqual(len(self.missions.pending_statement_dispatches()), 1)

    def test_a_checklist_that_throws_does_not_break_the_tick(self):
        def angry():
            raise RuntimeError("the driver is down")

        lane = MissionStatementLaneCoordinator(
            missions=self.missions, launcher=self.launcher, checklist=angry)
        self.assertEqual(lane.dispatch_once()["status"], "idle")

    def test_a_company_without_a_ticker_is_skipped(self):
        self.companies = [{"company_ref": ACN}, {"ticker": "ACN"}]
        self.assertEqual(self.lane.dispatch_once()["queued"], [])


if __name__ == "__main__":
    unittest.main()
