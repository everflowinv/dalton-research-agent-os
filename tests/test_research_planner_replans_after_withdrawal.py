"""2026-10-01: an owner withdrawal is a change in the research state.

Live 2026-09-30 21:05 UTC the owner withdrew 27 unrecoverable document-research
admissions in legacy (10 in ws-7d) so the planner could ask them again under
the current budget and contracts (commit f834c152).  Legacy never did:

* the coordinator's wake signature counted observations, Claims, documents --
  a withdrawal writes none of them, so the lane held "研究状态没有变化" for the
  next hour;
* the child's ``planning_core_hash`` stayed ``c81db7de`` all day, so it held
  ``no_material_change`` until the two-hour interval;
* and the fresh plans the interval bought (22:46, 01:03, 03:04) were never
  told: 25 of the 27 were admitted under earlier mission versions the
  per-version feedback never shows, and the other two still read
  ``recovery_required`` -- "execution is blocked".

ws-7d re-admitted within hours only because its checklist core happened to
move at 22:10 for unrelated reasons.  These tests pin that a withdrawal, or an
admission reaching a terminal outcome, buys exactly one fresh decision that
can see the reopened questions -- and that the equivalent-inquiry gate still
refuses a duplicate.
"""

from __future__ import annotations

import copy
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from dalton_core.document_inquiry_equivalence import ANSWERED_COOLDOWN
from dalton_core.document_research_feedback import (
    REOPENED_KEEP_PER_COMPANY, document_research_settlement_signature,
    read_document_research_settlement,
)
from dalton_core.mission_document_research import MissionDocumentResearchEquivalentPending
from dalton_core.mission_document_research_withdrawal import MissionDocumentWithdrawalAuthority
from dalton_core.research_planner import (
    build_prompt, project_state_for_prompt,
)
from dalton_core.research_planner_cli import (
    MIN_REPLAN_SECONDS, PLAN_ATTEMPT_LEDGER, PLAN_LAST_DECISION, SETTLEMENT_REPLAN_SECONDS,
    build_state, read_attempt_ledger, read_last_decision, replan_pacing_hold,
)
from dalton_core.research_planner_launcher import TICKET_PREFIX, ResearchPlannerCoordinator
from dalton_core.research_state import (
    planning_core_hash, planning_hash, planning_settlement_hash, state_content_hash,
)
# Imported as modules so unittest does not collect their TestCases twice.
from tests import test_mission_document_withdrawal as _withdrawal_fixtures
from tests import test_research_planner as _planner_fixtures
from tests import test_research_planner_cli as _cli_fixtures

UNRECOVERABLE = ("unrecoverable:output_cap_exhausted_below_current_budget: "
                 "起草回复在准入的输出上限里被用尽")


def _settlement(*, settlement_hash="s" * 64, reopened=()):
    ACN = _planner_fixtures.ACN
    return {
        "status": "available", "admissions": 3, "open": 1, "settled": 1,
        "withdrawn": 1, "settlement_hash": settlement_hash,
        "by_company": {ACN: {"reopened": list(reopened), "reopened_omitted": 0}},
    }


def _reopened(question="EPAM 2025 年收入按行业和地区怎么拆分？"):
    return {"admission_ref": "mission-document-research-admission:" + "a" * 32,
            "mission_version_ref": "coverage-mission-version:us-it-services:26",
            "question": question, "document_ref": "sec:filing:1",
            "withdrawn_at": "2026-09-30T21:05:08+00:00",
            "withdrawn_because": "output_cap_exhausted_below_current_budget",
            "equivalent_withdrawn": 2}


class _DocumentFixture:
    """One admitted directed read in a real Core, and the owner's door."""

    def build(self, case):
        _fixtures, helper = _withdrawal_fixtures._document_fixture(case)
        (self.fixture, self.authority, self.args,
         _registration, _launcher) = helper._fixture()
        self.helper = helper
        self.first = self.authority.admit_from_plan(**self.args)
        self.withdrawals = MissionDocumentWithdrawalAuthority(
            self.fixture.store, clock=self.fixture.harness.clock)
        return self

    def withdraw(self, admission_ref):
        return self.withdrawals.withdraw(
            admission_ref, actor_ref="human:owner", reason=UNRECOVERABLE)

    def rephrased(self, because):
        gate = _withdrawal_fixtures.EquivalentGateTests()
        gate.fixture, gate.helper, gate.args = self.fixture, self.helper, self.args
        return gate._rephrased_args(because=because)

    def answered(self, admission_ref, at):
        gate = _withdrawal_fixtures.EquivalentGateTests()
        gate.fixture = self.fixture
        gate._answered(admission_ref, at)

    def now(self):
        return self.fixture.harness.clock.value

    def read(self, *, at=None):
        moment = at or self.now()
        return read_document_research_settlement(
            self.fixture.store, self.fixture.mission,
            as_of=moment.isoformat(timespec="microseconds"))


class SettlementReaderTests(unittest.TestCase):
    def setUp(self):
        self.doc = _DocumentFixture().build(self)
        self.company = self.doc.first["company_ref"]

    def test_a_withdrawal_moves_the_settlement_once_and_reopens_the_question(self):
        before = self.doc.read()
        self.assertEqual((before["open"], before["settled"], before["withdrawn"]), (1, 0, 0))
        self.assertEqual(before["by_company"][self.company]["reopened"], [])
        # Read again with nothing settled: the same hash, every time.
        self.assertEqual(before["settlement_hash"], self.doc.read()["settlement_hash"])

        self.doc.withdraw(self.doc.first["id"])
        after = self.doc.read()
        self.assertNotEqual(before["settlement_hash"], after["settlement_hash"])
        self.assertEqual((after["open"], after["withdrawn"]), (0, 1))
        self.assertEqual(after["settlement_hash"], self.doc.read()["settlement_hash"])
        [row] = after["by_company"][self.company]["reopened"]
        self.assertEqual(row["admission_ref"], self.doc.first["id"])
        self.assertEqual(row["question"], self.doc.first["planner_inquiry"]["question"])
        self.assertEqual(row["withdrawn_because"], "output_cap_exhausted_below_current_budget")
        self.assertEqual(row["document_ref"],
                         self.doc.first["planner_inquiry"]["directed_document"]["document_ref"])

    def test_a_terminal_outcome_moves_the_settlement_without_reopening_anything(self):
        before = self.doc.read()
        self.doc.answered(self.doc.first["id"], self.doc.now())
        after = self.doc.read()
        self.assertNotEqual(before["settlement_hash"], after["settlement_hash"])
        self.assertEqual((after["open"], after["settled"]), (0, 1))
        self.assertEqual(after["by_company"][self.company]["reopened"], [])

    def test_only_what_the_equivalence_gate_would_admit_is_offered(self):
        self.doc.withdraw(self.doc.first["id"])
        # The planner asks it again (re-worded): the gate admits it as new
        # work, because a withdrawn equivalent never blocks...
        again = self.doc.authority.admit_from_plan(**self.doc.rephrased("re-asked"))
        self.assertEqual(again["status_marker"], "fresh")
        # ...and it is no longer "reopened": an equivalent is in flight, and
        # asking a third time is still refused by the same rule.
        self.assertEqual(self.doc.read()["by_company"][self.company]["reopened"], [])
        with self.assertRaises(MissionDocumentResearchEquivalentPending) as caught:
            self.doc.authority.admit_from_plan(**self.doc.rephrased("third time"))
        self.assertEqual(caught.exception.equivalent["admission_ref"], again["id"])
        # Answered: the cooldown keeps it closed, and after it the gate would
        # admit the question again, so it is offered again.
        now = self.doc.now()
        self.doc.answered(again["id"], now)
        self.assertEqual(self.doc.read(at=now)["by_company"][self.company]["reopened"], [])
        later = self.doc.read(at=now + ANSWERED_COOLDOWN)
        self.assertEqual([row["admission_ref"] for row in
                          later["by_company"][self.company]["reopened"]],
                         [self.doc.first["id"]])

    def test_the_coordinator_signature_wakes_on_a_withdrawal_and_an_outcome(self):
        quiet = document_research_settlement_signature(self.doc.fixture.store)
        self.assertEqual(quiet, document_research_settlement_signature(self.doc.fixture.store))
        self.doc.withdraw(self.doc.first["id"])
        withdrawn = document_research_settlement_signature(self.doc.fixture.store)
        self.assertNotEqual(quiet, withdrawn)
        again = self.doc.authority.admit_from_plan(**self.doc.rephrased("re-asked"))
        # An admission is not a settlement.
        self.assertEqual(withdrawn,
                         document_research_settlement_signature(self.doc.fixture.store))
        self.doc.answered(again["id"], self.doc.now())
        self.assertNotEqual(withdrawn,
                            document_research_settlement_signature(self.doc.fixture.store))

    def test_a_core_without_the_tables_is_nothing_settled_not_an_error(self):
        import sqlite3
        from types import SimpleNamespace
        bare = SimpleNamespace(connection=sqlite3.connect(":memory:"))
        self.addCleanup(bare.connection.close)
        mission = {"universe": [{"company_ref": "company:x"}]}
        read = read_document_research_settlement(bare, mission)
        self.assertEqual((read["status"], read["admissions"], read["withdrawn"]),
                         ("available", 0, 0))
        self.assertEqual(len(document_research_settlement_signature(bare)), 64)


class CoordinatorWakeTests(unittest.TestCase):
    """The lane held "研究状态没有变化" for an hour after the withdrawal."""

    def setUp(self):
        self.doc = _DocumentFixture().build(self)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.now = datetime(2026, 9, 30, 21, 0, tzinfo=timezone.utc)

        class Launcher:
            tickets_dir = Path(self.temp.name)
            starts = 0
            ticket = None

            def start(inner):
                inner.starts += 1
                inner.ticket = {"id": f"{TICKET_PREFIX}:{inner.starts:024x}",
                                "started_at": self.now.isoformat(), "status": "running",
                                "summary": None}
                return dict(inner.ticket)

            def status(inner, ticket_ref):
                return dict(inner.ticket)

        self.launcher = Launcher()
        self.coordinator = ResearchPlannerCoordinator(
            store=self.doc.fixture.store, launcher=self.launcher, clock=lambda: self.now)

    def finish(self):
        self.launcher.ticket.update({"status": "succeeded",
                                     "summary": {"status": "held", "plan_status": "held"}})

    def test_a_withdrawal_launches_the_child_on_the_next_tick(self):
        self.assertEqual(self.coordinator.dispatch_once()["status"], "launched")
        self.finish()
        self.now += timedelta(minutes=5)
        self.assertEqual(self.coordinator.dispatch_once()["status"], "held")
        self.doc.withdraw(self.doc.first["id"])
        self.now += timedelta(minutes=5)
        self.assertEqual(self.coordinator.dispatch_once()["status"], "launched")
        self.finish()
        # Once: the same settlement does not launch again.
        self.now += timedelta(minutes=5)
        self.assertEqual(self.coordinator.dispatch_once()["status"], "held")
        self.assertEqual(self.launcher.starts, 2)


class StateTests(unittest.TestCase):
    def test_a_settlement_is_material_but_not_core(self):
        state = _planner_fixtures.state
        none = state()
        quiet = state(document_research_settlement=_settlement(settlement_hash="a" * 64))
        moved = state(document_research_settlement=_settlement(
            settlement_hash="b" * 64, reopened=[_reopened()]))
        self.assertIsNone(planning_settlement_hash(none))
        self.assertEqual(planning_settlement_hash(moved), "b" * 64)
        self.assertNotEqual(planning_hash(quiet), planning_hash(moved))
        self.assertEqual(planning_core_hash(none), planning_core_hash(quiet))
        self.assertEqual(planning_core_hash(quiet), planning_core_hash(moved))
        self.assertNotEqual(quiet["content_hash"], moved["content_hash"])

    def test_the_planner_is_told_what_is_open_again(self):
        built = _planner_fixtures.state(document_research_settlement=_settlement(
            reopened=[_reopened()]))
        company = next(c for c in built["companies"]
                       if c["company_ref"] == _planner_fixtures.ACN)
        self.assertEqual(company["document_research_reopened"][0]["question"],
                         _reopened()["question"])
        self.assertNotIn("by_company", built["document_research_settlement"])
        prompt = build_prompt(built)
        self.assertIn("document_research_reopened", prompt)
        self.assertIn("Re-ask still-useful unresolved questions as new directed_document inquiries under current budget/gates; never revive old work", prompt)
        self.assertIn("equivalent pending/recently answered questions remain refused, not rewordable", prompt)
        self.assertIn(_reopened()["question"], prompt)

    def test_the_tightest_projection_keeps_the_reopened_questions(self):
        tests = _planner_fixtures.FeedbackProjectionTests(
            "test_twelve_companies_are_all_carried_at_the_live_bound")
        built = tests.worst_case(12)
        for company in built["companies"]:
            company["document_research_reopened"] = [
                _reopened(f"{company['ticker']} 问题 {n}")
                for n in range(REOPENED_KEEP_PER_COMPANY)]
            company["document_research_reopened_omitted"] = 3
        projected = project_state_for_prompt(tests.rebind(built), max_input_bytes=tests.BOUND)
        self.assertIn("compact_company_detail", projected["prompt_projection"]["stages_applied"])
        first = projected["companies"][0]
        self.assertEqual(len(first["document_research_reopened"]), REOPENED_KEEP_PER_COMPANY)
        self.assertEqual(first["document_research_reopened_omitted"], 3)


class PacingTests(unittest.TestCase):
    NOW = datetime(2026, 9, 30, 21, 10, tzinfo=timezone.utc)

    def last(self, minutes, **extra):
        return {"at": (self.NOW - timedelta(minutes=minutes)).isoformat(),
                "planning_hash": "p", "planning_core_hash": "c", **extra}

    def test_a_new_settlement_waits_only_for_the_short_floor(self):
        last = self.last(5, settlement_hash="old")
        held = replan_pacing_hold(last, "q", now=self.NOW, planning_core_hash="c",
                                  settlement_hash="new")
        self.assertEqual(held["held_reason"], "settlement_paced")
        floor = self.last(SETTLEMENT_REPLAN_SECONDS / 60, settlement_hash="old")
        self.assertIsNone(replan_pacing_hold(
            floor, "q", now=self.NOW, planning_core_hash="c", settlement_hash="new"))
        self.assertLess(SETTLEMENT_REPLAN_SECONDS, MIN_REPLAN_SECONDS)

    def test_the_same_settlement_never_buys_a_second_plan(self):
        last = self.last(SETTLEMENT_REPLAN_SECONDS / 60 + 1, settlement_hash="new")
        self.assertEqual(replan_pacing_hold(
            last, "p", now=self.NOW, planning_core_hash="c",
            settlement_hash="new")["held_reason"], "no_material_change")

    def test_a_decision_from_before_the_settlement_hash_sees_one_change(self):
        # The first tick after the upgrade folds legacy's 27 withdrawals into
        # one plan once the floor has passed.
        last = self.last(SETTLEMENT_REPLAN_SECONDS / 60)
        self.assertIsNone(replan_pacing_hold(
            last, "q", now=self.NOW, planning_core_hash="c", settlement_hash="new"))
        self.assertEqual(replan_pacing_hold(
            self.last(5), "q", now=self.NOW, planning_core_hash="c",
            settlement_hash="new")["held_reason"], "settlement_paced")

    def test_no_readable_settlement_never_counts_as_moved(self):
        last = self.last(5, settlement_hash="old")
        self.assertEqual(replan_pacing_hold(
            last, "p", now=self.NOW, planning_core_hash="c",
            settlement_hash=None)["held_reason"], "no_material_change")

    def test_a_core_change_is_still_asked_at_once(self):
        last = self.last(1, settlement_hash="old")
        self.assertIsNone(replan_pacing_hold(
            last, "q", now=self.NOW, planning_core_hash="d", settlement_hash="new"))


class PlannerRunTests(unittest.TestCase):
    """The child, end to end: one fresh decision per settlement, no churn."""

    setUp = _cli_fixtures.PlannerChildTests.setUp
    close = _cli_fixtures.PlannerChildTests.close
    publish_mission = _cli_fixtures.PlannerChildTests.publish_mission
    plan = _cli_fixtures.PlannerChildTests.plan

    def _age_last_decision(self, seconds):
        """Let ``seconds`` pass: for the last decision and the attempt ledger.

        Live the coordinator holds an unmoved signature for an hour, so the
        child that runs after a ``settlement_paced`` hold is past both the
        floor and the ledger's own backoff for that exact state.
        """

        path = self.state / PLAN_LAST_DECISION
        last = read_last_decision(self.state)
        last["at"] = (datetime.fromisoformat(last["at"])
                      - timedelta(seconds=seconds)).isoformat()
        path.write_text(json.dumps(last), encoding="utf-8")
        ledger_path = self.state / PLAN_ATTEMPT_LEDGER
        ledger = read_attempt_ledger(self.state)
        for record in ledger.values():
            record["at"] = (datetime.fromisoformat(record["at"])
                            - timedelta(seconds=seconds)).isoformat()
        ledger_path.write_text(json.dumps(ledger), encoding="utf-8")

    def test_a_withdrawal_buys_one_plan_after_the_floor_and_then_holds(self):
        mission = self.publish_mission()
        base = build_state(self.store, self.missions, mission,
                           plans_dir=self.state / "discovery-plans",
                           as_of="2026-09-30T21:00:00+00:00")
        self.assertEqual(base["document_research_settlement"]["status"], "available")
        before_hash = planning_settlement_hash(base)
        self.assertIsNotNone(before_hash)

        def variant(settlement_hash, as_of):
            moved = copy.deepcopy(base)
            moved["as_of"] = as_of
            moved["document_research_settlement"]["settlement_hash"] = settlement_hash
            moved["document_research_settlement"]["withdrawn"] = 27
            moved["content_hash"] = state_content_hash(moved)
            return moved

        withdrawn = variant("w" * 64, "2026-09-30T21:05:10+00:00")
        config = self.root / "model.json"
        config.write_text("{}", encoding="utf-8")
        reply = json.dumps({"schema_version": "0.1", "assessment": "Hold course.",
                            "directives": [], "inquiries": [], "sufficiency": []})
        results = []
        with patch("dalton_core.research_planner_cli.CockpitModel") as model:
            model.return_value.budget_for.return_value = {
                "max_input_tokens": 80_000, "max_output_tokens": 4_000,
                "max_cost_usd": 1.0, "timeout_seconds": 300}
            model.return_value.call.return_value = {
                "text": reply, "cost_micros": 440_000, "replayed": False}

            def run(built):
                with patch("dalton_core.research_planner_cli.build_state",
                           return_value=built):
                    results.append(self.plan(dry_run=False, model_config_path=config))

            run(base)                       # the 19:57-style plan
            run(withdrawn)                  # minutes later: paced, not ignored
            self._age_last_decision(SETTLEMENT_REPLAN_SECONDS)
            run(withdrawn)                  # after the floor: one fresh plan
            again = copy.deepcopy(withdrawn)
            again["as_of"] = "2026-09-30T22:10:00+00:00"
            run(again)                      # the same state replays its plan
            noise = copy.deepcopy(again)
            noise["totals"]["figures_held"] += 1
            noise["content_hash"] = state_content_hash(noise)
            self._age_last_decision(SETTLEMENT_REPLAN_SECONDS)
            run(noise)                      # same settlement, new counters: no churn
            calls = model.return_value.call.call_count

        self.assertEqual([item["plan_status"] for item in results],
                         ["fresh", "held", "fresh", "unchanged", "held"])
        self.assertEqual(results[1]["held_reason"], "settlement_paced")
        self.assertEqual(results[4]["held_reason"], "no_material_change")
        self.assertEqual(calls, 2)
        self.assertEqual(read_last_decision(self.state)["settlement_hash"], "w" * 64)
        self.assertEqual(results[0]["planning_core_hash"], results[2]["planning_core_hash"])


if __name__ == "__main__":
    unittest.main()
