"""P13ak: the statements lane on a tick -- queue, launch, settle, record.

The connector worked by hand before this existed. What these tests hold to
account is the part that runs unattended: that a finished child's numbers reach
the ledger, that a company whose runs keep failing stops consuming the single
slot, and that a rejection is not permanent.
"""

from __future__ import annotations

import unittest
from datetime import datetime, timedelta, timezone

from dalton_core.company_model_spec import spec_from_response
from dalton_core.coverage_mission import (
    MAX_STATEMENT_FILINGS,
    CoverageMissionAuthority,
)
from dalton_core.lane_child_launcher import (
    LaneChildConflict,
    LaneChildRejected,
    LaneChildTicketNotFound,
)
from dalton_core.mission_statement_lane import (
    CONFIGURATION_HOLD_SECONDS,
    MAX_ATTEMPTS_PER_COMPANY,
    MAX_FAILURES_PER_COMPANY,
    MissionStatementLaneCoordinator,
)
from dalton_core.store import DaltonStore
from tests.p9a_fixtures import bootstrap_method_authorities, mission_params

ACN = "company:sec-cik:0001467373"
ACCESSION = "0001467373-26-000031"


def _model_spec(*, historical_quarters):
    """The smallest specification the frame accepts, with a chosen horizon."""

    return {
        "schema_version": "0.3",
        "revenue_anchor_concept": "us-gaap:Revenues",
        "assessment": "A people business: heads times realised rate.",
        "revenue_drivers": [{
            "ref": "heads", "label": "Billable headcount", "kind": "volume",
            "basis_concept": None, "unit": "headcount",
            "because": "Capacity binds delivery revenue.",
        }],
        "expense_lines": [{
            "ref": "delivery", "label": "Cost of services",
            "basis_concept": None,
            "behaviour": "variable_with_headcount", "driver_ref": "heads",
            "because": "Delivery payroll follows the billable base.",
        }],
        "forecast_statements": [
            {"statement": "income", "importance": "required",
             "because": "Revenue and margin are the question."},
            {"statement": "balance", "importance": "supporting",
             "because": "Capital light."},
            {"statement": "cash", "importance": "supporting",
             "because": "Conversion is steady."},
        ],
        "operating_metrics": [],
        "horizon": {
            "historical_quarters": historical_quarters, "forecast_quarters": 8,
            "because": "What this company's cycle needs.",
        },
        "financial_statement_structure": {
            "schema_version": "0.1",
            "lines": [{
                "ref": "revenue", "role": "revenue", "label": "Revenue",
                "kind": "filed", "concept": "us-gaap:Revenues",
                "statement": "income", "unit": "usd", "period_kind": "duration",
                "annual_semantics": "sum_quarters",
                "forecast_method": "quarterly_growth", "forecast_base_ref": None,
            }],
            "formulas": [],
        },
    }


def _observation(accession=ACCESSION, *, form="10-Q", report_date="2026-06-30"):
    return {
        "schema_version": "0.1", "cik": "0001467373", "entity_name": "Accenture plc",
        "filings": [{
            "accession": accession, "form": form, "filed": "2026-06-25",
            "report_date": report_date,
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
        # The ledger stamps rows from the real clock, so the lane's clock
        # starts there and the test moves it forward.
        self.now = datetime.now(timezone.utc)
        self.lane = MissionStatementLaneCoordinator(
            missions=self.missions, launcher=self.launcher,
            checklist=lambda: self.companies, clock=lambda: self.now,
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
        self.launcher.raise_on_start = LaneChildRejected(
            "SEC governance record is not approved")
        first = self.lane.dispatch_once()
        self.assertEqual(first["status"], "rejected")
        self.launcher.raise_on_start = None
        # The fault was this Core's, not the company's, so the lane holds
        # rather than asking again on the very next tick.
        held = self.lane.dispatch_once()
        self.assertEqual(held["status"], "idle")
        self.assertEqual(held["queued"][0]["status"], "held")
        # And once the window is over it picks itself up: a retry is a distinct
        # dispatch, so the earlier rejection does not freeze this company out.
        self.now += timedelta(seconds=CONFIGURATION_HOLD_SECONDS + 1)
        self.assertEqual(self.lane.dispatch_once()["status"], "launched")

    def test_our_own_misconfiguration_does_not_spend_a_company_budget(self):
        # The live first tick: every child died on a malformed EDGAR identity.
        # Counted as ordinary failures, three ticks would have exhausted every
        # company and left the lane idle for good once the identity was fixed.
        for _ in range(MAX_FAILURES_PER_COMPANY + 2):
            launched = self.lane.dispatch_once()
            self.assertEqual(launched["status"], "launched")
            self.launcher.finish(
                launched["ticket_ref"], status="failed",
                summary={"failure_reason": "SECIdentityError: missing EDGAR_IDENTITY"})
            self.lane.dispatch_once()
            self.now += timedelta(seconds=CONFIGURATION_HOLD_SECONDS + 1)
        # Fixed. The company still has its full budget, and the next run works.
        launched = self.lane.dispatch_once()
        self.launcher.finish(launched["ticket_ref"], summary=self.succeeded_summary())
        self.lane.dispatch_once()
        self.assertEqual(
            [item["accession"] for item in self.missions.statement_filings(ACN)],
            [ACCESSION])

    def test_a_company_that_keeps_failing_stops_consuming_the_slot(self):
        # A failure that is genuinely about this company's filings: it did not
        # file, or what it filed carries no XBRL.
        for _ in range(MAX_FAILURES_PER_COMPANY):
            launched = self.lane.dispatch_once()
            self.assertEqual(launched["status"], "launched")
            self.launcher.finish(launched["ticket_ref"], status="failed", summary={
                "failure_reason":
                    "SecFinancialsRunError: the parser returned no filing with XBRL"})
        exhausted = self.lane.dispatch_once()
        self.assertEqual(exhausted["queued"], [])
        self.assertEqual(exhausted["status"], "idle")
        self.assertEqual(len(self.launcher.started), MAX_FAILURES_PER_COMPANY)

    def test_a_bug_in_our_own_adapter_is_not_charged_to_the_company(self):
        # Live: an AttributeError in this codebase -- asking a collection of
        # filings for the XBRL only a single filing has -- spent IBM's entire
        # retry budget in three ticks on something that had nothing to do with
        # IBM. A failure this system cannot attribute to the company is ours.
        for _ in range(MAX_FAILURES_PER_COMPANY + 2):
            launched = self.lane.dispatch_once()
            self.assertEqual(launched["status"], "launched")
            self.launcher.finish(launched["ticket_ref"], status="failed", summary={
                "failure_reason":
                    "AttributeError: 'EntityFilings' object has no attribute 'xbrl'"})
            self.lane.dispatch_once()
            self.now += timedelta(seconds=CONFIGURATION_HOLD_SECONDS + 1)
        # Fixed. The company still has its budget and the next run works.
        launched = self.lane.dispatch_once()
        self.launcher.finish(launched["ticket_ref"], summary=self.succeeded_summary())
        self.lane.dispatch_once()
        self.assertEqual(
            [item["accession"] for item in self.missions.statement_filings(ACN)],
            [ACCESSION])

    def test_a_fault_that_survives_every_attempt_is_finally_left_alone(self):
        for _ in range(MAX_ATTEMPTS_PER_COMPANY):
            launched = self.lane.dispatch_once()
            self.assertEqual(launched["status"], "launched")
            self.launcher.finish(launched["ticket_ref"], status="failed",
                                 summary={"failure_reason": "OSError: something odd"})
            self.lane.dispatch_once()
            self.now += timedelta(seconds=CONFIGURATION_HOLD_SECONDS + 1)
        done = self.lane.dispatch_once()
        self.assertEqual(done["status"], "idle")
        self.assertIn("not trying again", done["queued"][0]["reason"])
        self.assertEqual(len(self.launcher.started), MAX_ATTEMPTS_PER_COMPANY)

    def test_a_failure_with_no_reason_is_not_evidence_against_the_company(self):
        launched = self.lane.dispatch_once()
        self.launcher.finish(launched["ticket_ref"], status="failed", summary=None)
        held = self.lane.dispatch_once()
        self.assertEqual(held["queued"][0]["status"], "held")

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

    def test_history_depth_comes_from_the_company_own_model(self):
        # P13am: IBM's specification asked for twenty quarters to separate
        # mainframe launch cycles from the underlying business. One quarter is
        # only the floor for a company that has no specification yet.
        launched = self.lane.dispatch_once()
        self.assertEqual(self.launcher.started[0]["limit"], 1)
        self.launcher.finish(launched["ticket_ref"], summary=self.succeeded_summary())
        spec = spec_from_response(
            {"company_ref": ACN, "state_hash": "a" * 64,
             "filings": [{"accession": ACCESSION}],
             "concepts": ["us-gaap:Revenues"],
             "statements": {"income": _observation()["filings"][0]["lines"]}},
            _model_spec(historical_quarters=20), decided_by="automation:x")
        self.missions.record_company_model_spec(
            spec, mission_version_ref=self.mission["id"])
        deeper = self.lane.dispatch_once()
        # Bounded by what one child may parse in a run, not by what was asked.
        self.assertEqual(self.launcher.started[-1]["limit"], MAX_STATEMENT_FILINGS)
        self.assertEqual(deeper["settled"][0]["outcome"], "succeeded")

    def test_a_specification_asking_for_less_never_goes_below_the_floor(self):
        spec = spec_from_response(
            {"company_ref": ACN, "state_hash": "a" * 64,
             "filings": [{"accession": ACCESSION}],
             "concepts": ["us-gaap:Revenues"],
             "statements": {"income": _observation()["filings"][0]["lines"]}},
            _model_spec(historical_quarters=1), decided_by="automation:x")
        self.missions.record_company_model_spec(
            spec, mission_version_ref=self.mission["id"])
        self.lane.dispatch_once()
        self.assertEqual(self.launcher.started[0]["limit"], 1)

    def test_a_company_without_a_ticker_is_skipped(self):
        self.companies = [{"company_ref": ACN}, {"ticker": "ACN"}]
        self.assertEqual(self.lane.dispatch_once()["queued"], [])

    def test_production_forms_backfill_annual_before_covered_quarterly(self):
        lane = MissionStatementLaneCoordinator(
            missions=self.missions, launcher=self.launcher,
            checklist=lambda: self.companies, forms=("10-K", "10-Q"),
            clock=lambda: self.now,
        )
        launched = lane.dispatch_once()
        self.assertEqual(launched["status"], "launched")
        self.assertEqual(self.launcher.started[-1]["form"], "10-K")
        annual = _observation(
            "0001467373-25-000099", form="10-K", report_date="2025-08-31")
        self.launcher.finish(launched["ticket_ref"], summary=self.succeeded_summary(annual))
        following = lane.dispatch_once()
        self.assertEqual(following["settled"][0]["outcome"], "succeeded")
        self.assertEqual(self.launcher.started[-1]["form"], "10-Q")

    def test_annual_failures_do_not_spend_quarterly_attempts(self):
        lane = MissionStatementLaneCoordinator(
            missions=self.missions, launcher=self.launcher,
            checklist=lambda: self.companies, forms=("10-K", "10-Q"),
            clock=lambda: self.now,
        )
        launched = lane.dispatch_once()
        for _ in range(MAX_FAILURES_PER_COMPANY):
            self.assertEqual(self.launcher.started[-1]["form"], "10-K")
            self.launcher.finish(launched["ticket_ref"], status="failed", summary={
                "failure_reason":
                    "SecFinancialsRunError: the parser returned no filing with XBRL"})
            launched = lane.dispatch_once()
        self.assertEqual(self.launcher.started[-1]["form"], "10-Q")

    def test_annual_configuration_hold_does_not_block_quarterly(self):
        lane = MissionStatementLaneCoordinator(
            missions=self.missions, launcher=self.launcher,
            checklist=lambda: self.companies, forms=("10-K", "10-Q"),
            filing_limits={"10-K": 1}, clock=lambda: self.now,
        )
        launched = lane.dispatch_once()
        self.launcher.finish(launched["ticket_ref"], status="failed", summary={
            "failure_reason": "OSError: temporary parser configuration failure"})
        following = lane.dispatch_once()
        self.assertEqual(following["status"], "launched")
        self.assertEqual(self.launcher.started[-1]["form"], "10-Q")
        self.assertTrue(any(
            item.get("form") == "10-K" and item["status"] == "held"
            for item in following["queued"]
        ))

    def test_per_form_floor_is_configurable(self):
        lane = MissionStatementLaneCoordinator(
            missions=self.missions, launcher=self.launcher,
            checklist=lambda: self.companies, forms=("10-K",),
            filing_limits={"10-K": 2}, clock=lambda: self.now,
        )
        lane.dispatch_once()
        self.assertEqual(self.launcher.started[-1], {
            "ticker": "ACN", "form": "10-K", "limit": 2,
            "actor_ref": "automation:coverage-mission",
        })


if __name__ == "__main__":
    unittest.main()
