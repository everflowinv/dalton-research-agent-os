"""P13am: the specification lane on a tick.

It has no queue. What needs deciding is derived from the ledger each tick -- a
company with filed statements and no specification for the structure those
filings disclose -- so its resting state is silence and there is nothing to
leave stuck. What these tests hold to account is that it settles the previous
tick's child before starting another, that a failed judgement does not consume
the slot every five minutes, and that it goes quiet once every company has one.
"""

from __future__ import annotations

import unittest

from dalton_core.lane_child_launcher import LaneChildConflict, LaneChildTicketNotFound
import dalton_core.mission_model_spec_lane as model_spec_lane
from dalton_core.mission_model_spec_lane import MissionModelSpecLaneCoordinator


class FakeLauncher:
    def __init__(self):
        self.tickets: dict[str, dict] = {}
        self.started: list[dict] = []
        self.raise_on_start: Exception | None = None

    def start(self, *, company_ref, state_hash, task_hash=None):
        if self.raise_on_start is not None:
            raise self.raise_on_start
        self.started.append({"company_ref": company_ref, "state_hash": state_hash,
                             "task_hash": task_hash})
        ticket_id = f"company-model-spec-run:{len(self.started):024d}"
        self.tickets[ticket_id] = {
            "id": ticket_id, "status": "running", "summary": None,
            "company_ref": company_ref, "state_hash": state_hash,
            "task_hash": task_hash,
        }
        return {"id": ticket_id}

    def finish(self, ticket_id, *, status="succeeded", summary=None):
        self.tickets[ticket_id].update({"status": status, "summary": summary})

    def status(self, ticket_ref):
        if ticket_ref not in self.tickets:
            raise LaneChildTicketNotFound(ticket_ref)
        return self.tickets[ticket_ref]


class FakeMissions:
    """Just enough ledger for the coordinator: who has filings, who has specs."""

    def __init__(self, companies):
        self.companies = list(companies)
        self.specs: dict[tuple[str, str], dict] = {}
        # Per company, because the state hash the coordinator keys on is
        # computed from these by the real projection -- so "this company filed
        # something new" has to be a real change in what it discloses.
        self.lines = {ref: ["us-gaap:Revenues"] for ref in self.companies}

    def statement_filings(self, company_ref=None):
        refs = self.companies if company_ref is None else [company_ref]
        return [{"company_ref": ref, "ingest_id": f"ingest:{ref}",
                 "entity_name": ref, "cik": "0000000001", "accession": "a",
                 "form": "10-Q", "report_date": "2026-06-30", "line_count": 1}
                for ref in refs if ref in self.companies]

    def statement_lines(self, ingest_id, statement=None):
        company_ref = ingest_id.split(":", 1)[1]
        return [{"statement": "income", "concept": concept,
                 "label": concept.split(":")[-1], "level": 0,
                 "parent_concept": None, "is_breakdown": 0,
                 "dimension_axis": None, "dimension_member": None}
                for concept in self.lines.get(company_ref, [])]

    def discloses(self, company_ref, concept):
        """This company filed something nobody had seen before."""

        self.lines[company_ref] = self.lines[company_ref] + [concept]

    def company_model_spec_for_state(self, company_ref, state_hash, *, task_hash=None):
        return self.specs.get((company_ref, state_hash))

    def record(self, company_ref, state_hash):
        self.specs[(company_ref, state_hash)] = {"spec_id": "spec:1"}


ACN = "company:sec-cik:0001467373"
IBM = "company:sec-cik:0000051143"


class ModelSpecLaneTests(unittest.TestCase):
    def setUp(self) -> None:
        self.missions = FakeMissions([ACN, IBM])
        self.launcher = FakeLauncher()
        self.mission = {
            "id": "coverage-mission-version:test:1",
            "universe": [{"company_ref": ACN, "ticker": "ACN"},
                         {"company_ref": IBM, "ticker": "IBM"}],
        }
        self.lane = MissionModelSpecLaneCoordinator(
            missions=self.missions, launcher=self.launcher,
            mission=lambda: self.mission)

    def state_hash_of(self, ticket_ref):
        return self.launcher.tickets[ticket_ref]["state_hash"]

    def succeed(self, ticket_ref, company_ref):
        self.missions.record(company_ref, self.state_hash_of(ticket_ref))
        self.launcher.finish(ticket_ref, summary={
            "spec_status": "fresh", "spec_ref": "spec:1",
            "cost_micros": 241710, "revenue_drivers": 8})

    def test_a_tick_decides_about_one_company(self):
        result = self.lane.dispatch_once()
        self.assertEqual(result["status"], "launched")
        self.assertEqual(result["company_ref"], ACN)
        self.assertEqual(len(self.launcher.started), 1)

    def test_the_next_tick_settles_the_last_child_before_starting_another(self):
        first = self.lane.dispatch_once()
        self.succeed(first["ticket_ref"], ACN)
        second = self.lane.dispatch_once()
        self.assertEqual(second["settled"]["spec_status"], "fresh")
        self.assertEqual(second["settled"]["cost_micros"], 241710)
        self.assertEqual(second["company_ref"], IBM)

    def test_the_lane_goes_quiet_once_every_company_has_one(self):
        for company in (ACN, IBM):
            launched = self.lane.dispatch_once()
            self.succeed(launched["ticket_ref"], company)
        quiet = self.lane.dispatch_once()
        self.assertEqual(quiet["status"], "idle")
        self.assertIn("current specification", quiet["reason"])
        self.assertEqual(len(self.launcher.started), 2)

    def test_a_new_disclosure_brings_that_company_back(self):
        for company in (ACN, IBM):
            launched = self.lane.dispatch_once()
            self.succeed(launched["ticket_ref"], company)
        self.assertEqual(self.lane.dispatch_once()["status"], "idle")
        # ACN files something nobody had seen: its structure moves, its old
        # specification becomes history rather than wrong, and exactly that
        # company is decided about again.
        self.missions.discloses(ACN, "acn:NewBookings")
        back = self.lane.dispatch_once()
        self.assertEqual(back["status"], "launched")
        self.assertEqual(back["company_ref"], ACN)
        self.assertNotEqual(back["state_hash"],
                            self.launcher.started[0]["state_hash"])

    def test_a_failed_judgement_does_not_consume_the_slot_every_tick(self):
        launched = self.lane.dispatch_once()
        self.launcher.finish(launched["ticket_ref"], summary={
            "spec_status": "refused",
            "failure_reason": "CompanyModelSpecError: not a concept"})
        held = self.lane.dispatch_once()
        self.assertEqual(held["status"], "held")
        self.assertIn("not a concept", held["reason"])
        self.assertEqual(held["settled"]["spec_status"], "refused")
        # And it stays held rather than being retried on the next tick.
        self.assertEqual(self.lane.dispatch_once()["status"], "held")
        self.assertEqual(len(self.launcher.started), 1)

    def test_an_old_contract_failure_does_not_hold_a_new_contract(self):
        launched = self.lane.dispatch_once()
        self.launcher.finish(launched["ticket_ref"], summary={
            "spec_status": "refused", "failure_reason": "old contract"})
        self.assertEqual(self.lane.dispatch_once()["status"], "held")
        old_hash = model_spec_lane.TASK_HASH
        self.addCleanup(setattr, model_spec_lane, "TASK_HASH", old_hash)
        model_spec_lane.TASK_HASH = "f" * 64
        resumed = self.lane.dispatch_once()
        self.assertEqual(resumed["status"], "launched")
        self.assertEqual(self.launcher.started[-1]["task_hash"], "f" * 64)

    def test_a_child_that_died_without_a_summary_spends_one_transient_retry(self):
        launched = self.lane.dispatch_once()
        self.launcher.finish(launched["ticket_ref"], status="failed", summary=None)
        retried = self.lane.dispatch_once()
        self.assertEqual(retried["status"], "launched")
        self.assertEqual(retried["settled"]["failure"]["failure_class"], "transient")
        self.assertEqual(retried["settled"]["failure"]["failures"], 1)

    def test_a_running_child_is_left_alone(self):
        self.lane.dispatch_once()
        self.launcher.raise_on_start = LaneChildConflict("already running")
        busy = self.lane.dispatch_once()
        self.assertEqual(busy["status"], "busy")
        self.assertEqual(busy["settled"]["status"], "running")

    def test_dependency_failure_retries_same_state_as_probe(self):
        first = self.lane.dispatch_once()
        self.launcher.finish(first["ticket_ref"], summary={
            "spec_status": "model_unavailable", "failure_reason": "model_unavailable"})
        probe = self.lane.dispatch_once()
        self.assertEqual(probe["status"], "launched")
        self.assertEqual(probe["state_hash"], first["state_hash"])
        self.assertEqual(probe["settled"]["failure"]["failure_class"],
                         "dependency_unavailable")

    def test_no_mission_is_reported_not_crashed(self):
        lane = MissionModelSpecLaneCoordinator(
            missions=self.missions, launcher=self.launcher, mission=lambda: None)
        self.assertEqual(lane.dispatch_once()["status"], "unconfigured")

    def test_an_unreadable_ledger_does_not_break_the_tick(self):
        class Angry(FakeMissions):
            def statement_filings(self, company_ref=None):
                raise RuntimeError("the ledger is locked")

        lane = MissionModelSpecLaneCoordinator(
            missions=Angry([ACN]), launcher=self.launcher,
            mission=lambda: self.mission)
        result = lane.dispatch_once()
        self.assertEqual(result["status"], "unavailable")
        self.assertIn("the ledger is locked", result["reason"])

    def test_a_company_outside_the_universe_is_not_decided_about(self):
        stranger = "company:sec-cik:0009999999"
        self.missions.companies.append(stranger)
        self.missions.lines[stranger] = ["us-gaap:Revenues"]
        for company in (ACN, IBM):
            launched = self.lane.dispatch_once()
            self.succeed(launched["ticket_ref"], company)
        self.assertEqual(self.lane.dispatch_once()["status"], "idle")


if __name__ == "__main__":
    unittest.main()
