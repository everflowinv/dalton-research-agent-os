"""The owner's withdraw door and the equivalent-inquiry rule (2026-09-30).

Admissions are append-only and the only owner doors used to re-open a stage,
so a hold no retry could fix waited on a person for ever (legacy ~30, ws-7d
~10), and every plan's rephrasing of the same question was bought again
(legacy DXC "970 / 9.7 亿" ~17 times).  These tests pin:

* the withdrawal ledger: human-only, one per admission, append-only;
* that ``resolve_for_execution`` and the executor refuse a withdrawn admission;
* that the same inquiry may be admitted again afterwards, as new authority;
* the lane: a withdrawn hold becomes terminal and stops waiting on the owner;
* the writer door's refusals (hold not escalated/terminal, child running,
  WorkOrder leased) and the CLI's read-only classification of hopeless holds;
* the equivalent-inquiry rule, both as a pure function and at the gate.
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from dalton_core.document_inquiry_equivalence import (
    ANSWERED_COOLDOWN, inquiry_signature, question_figures, signatures_equivalent,
)
from dalton_core.mission_document_research import (
    MissionDocumentResearchEquivalentPending, MissionDocumentResearchWithdrawn,
)
from dalton_core.mission_document_research_withdrawal import (
    MissionDocumentWithdrawalAuthority, MissionDocumentWithdrawalError, WITHDRAWN_REASON,
    read_withdrawals, running_work_orders, withdrawable_hold,
)
from dalton_core.store import canonical_json, content_hash


def _document_fixture(case: unittest.TestCase):
    from tests import test_mission_document_research as fixtures

    helper = fixtures.MissionDocumentResearchTests()
    helper.addCleanup = case.addCleanup
    helper.fail = case.fail
    helper.assertEqual = case.assertEqual
    return fixtures, helper


class EquivalenceRuleTests(unittest.TestCase):
    def test_the_live_rephrasings_are_one_question(self):
        company = "company:sec-cik:001688568"
        asked = [
            "DXC 档案供给与成本部分的 970 指什么金额，对应哪个期间？",
            "DXC 档案「供给与成本」一节里的 9.7 亿美元，对应 10-K 的哪个科目、哪个财年？",
            "Is the $970 million in DXC's FY2026 10-K a restructuring cost?",
            "What is the $0.97 billion figure?",
        ]
        first = inquiry_signature(company, asked[0])
        for question in asked[1:]:
            with self.subTest(question=question):
                self.assertEqual(signatures_equivalent(
                    first, inquiry_signature(company, question)), "figure:97")

    def test_what_is_not_a_figure(self):
        self.assertEqual(question_figures(
            "AMZN 的 5 份 SEC 申报（0001018724-26-000036）FY2025 10-K 与 2024 年 Q3 20-F"),
            frozenset())
        self.assertEqual(question_figures("CTSH 15.8% 与 5,000 和 4,000"),
                         frozenset({"158", "5", "4"}))

    def test_what_is_never_equivalent(self):
        a = inquiry_signature("company:a", "What is the 15.8% margin?")
        self.assertIsNone(signatures_equivalent(
            a, inquiry_signature("company:b", "What is the 15.8% margin?")))
        self.assertIsNone(signatures_equivalent(
            a, inquiry_signature("company:a", "What does the 77.7 refer to?")))
        self.assertIsNone(signatures_equivalent(
            inquiry_signature("company:a", "How is revenue recognized?"),
            inquiry_signature("company:a", "How is revenue recognised over time?")))
        self.assertIsNone(signatures_equivalent(
            inquiry_signature("company:a", "What is the 15.8% margin?"),
            inquiry_signature("company:a", "What is the 15.8% margin?",
                              {"target_ref": "financial-note-target:x"})))
        self.assertEqual(signatures_equivalent(
            inquiry_signature("company:a", "How is Revenue recognized?"),
            inquiry_signature("company:a", "how is revenue  recognized")), "question")


class WithdrawalAuthorityTests(unittest.TestCase):
    def setUp(self):
        self.fixtures, self.helper = _document_fixture(self)
        (self.fixture, self.authority, self.args,
         _registration, _launcher) = self.helper._fixture()
        self.admission = self.authority.admit_from_plan(**self.args)
        self.withdrawals = MissionDocumentWithdrawalAuthority(
            self.fixture.store, clock=self.fixture.harness.clock)
        self.hold = {"disposition": "recovery_required",
                     "reason": "contract_failed_after_automatic_retry",
                     "ticket_ref": None}

    def test_only_a_person_with_a_reason_may_withdraw_an_exact_admission(self):
        for kwargs, message in (
            ({"actor_ref": "automation:lane", "reason": "x"}, "human actor"),
            ({"actor_ref": "human:", "reason": "x"}, "human actor"),
            ({"actor_ref": "human:owner", "reason": "  "}, "reason"),
        ):
            with self.subTest(message=message), self.assertRaisesRegex(
                    MissionDocumentWithdrawalError, message):
                self.withdrawals.withdraw(self.admission["id"], **kwargs)
        with self.assertRaisesRegex(MissionDocumentWithdrawalError, "unavailable"):
            self.withdrawals.withdraw("mission-document-research-admission:" + "0" * 32,
                                      actor_ref="human:owner", reason="x")

    def test_the_ledger_is_append_only_and_replays_its_own_row(self):
        first = self.withdrawals.withdraw(self.admission["id"], actor_ref="human:owner",
                                          reason="door already used", hold=self.hold)
        self.assertEqual(first["status"], "withdrawn")
        again = self.withdrawals.withdraw(self.admission["id"], actor_ref="human:other",
                                          reason="again")
        self.assertEqual(again["status"], "already_withdrawn")
        self.assertEqual(again["id"], first["id"])
        connection = self.fixture.store.connection
        for statement in ("UPDATE mission_document_research_withdrawals SET reason='x'",
                          "DELETE FROM mission_document_research_withdrawals"):
            with self.subTest(statement=statement), self.assertRaises(sqlite3.DatabaseError):
                connection.execute(statement)
        with self.assertRaises(sqlite3.DatabaseError):
            connection.execute(
                "INSERT INTO mission_document_research_withdrawals VALUES(?,?,?,?,?,?,?,?)",
                ("w:2", "mission-document-research-admission:" + "1" * 32, "a" * 64,
                 "human:owner", "x", "{}", "b" * 64, "2026-09-30"))
        record = read_withdrawals(connection)[self.admission["id"]]
        self.assertEqual(record["hold"]["reason"], "contract_failed_after_automatic_retry")
        self.assertEqual(record["admission_hash"], self.admission["content_hash"])

    def test_a_withdrawn_admission_is_refused_by_resolve_and_by_the_executor(self):
        executor, draft, verifier = self.helper._executor(self.fixture, self.authority)
        self.withdrawals.withdraw(self.admission["id"], actor_ref="human:owner", reason="x")
        with self.assertRaises(MissionDocumentResearchWithdrawn):
            self.authority.resolve_for_execution(self.admission["id"])
        with self.assertRaisesRegex(Exception, "withdrawn by its owner"):
            executor.run_once(self.admission["id"])
        self.assertEqual((draft.calls, verifier.calls), (0, 0))

    def test_the_same_inquiry_may_be_admitted_again_as_new_authority(self):
        from dalton_core.mission_document_research_executor import _blueprints

        self.withdrawals.withdraw(self.admission["id"], actor_ref="human:owner", reason="x")
        again = self.authority.admit_from_plan(**self.args)
        self.assertEqual(again["status_marker"], "fresh")
        self.assertNotEqual(again["id"], self.admission["id"])
        self.assertEqual(again["supersedes_withdrawal"]["admission_ref"], self.admission["id"])
        self.assertEqual(self.authority.resolve_for_execution(again["id"])["id"], again["id"])
        self.assertEqual(self.authority.admit_from_plan(**self.args)["status_marker"],
                         "duplicate")
        old_ids = {work["id"] for work in _blueprints(self.admission)}
        new_ids = {work["id"] for work in _blueprints(again)}
        self.assertFalse(old_ids & new_ids)
        executor, _draft, _verifier = self.helper._executor(self.fixture, self.authority)
        for _ in range(30):
            if executor.run_once(again["id"])["status"] == "complete":
                break
        else:
            self.fail("the re-admitted inquiry did not complete")


class EquivalentGateTests(unittest.TestCase):
    def setUp(self):
        self.fixtures, self.helper = _document_fixture(self)
        (self.fixture, self.authority, self.args,
         _registration, _launcher) = self.helper._fixture()
        self.first = self.authority.admit_from_plan(**self.args)

    def _rephrased_args(self, *, because="a second plan's search terms"):
        from dalton_core.research_task import inquiry_content_hash, inquiry_ref_for

        old = json.loads(self.fixture.store.connection.execute(
            "SELECT plan_json FROM coverage_mission_research_plans WHERE plan_id=?",
            (self.args["plan_ref"],)).fetchone()[0])
        plan = {key: value for key, value in old.items() if key != "content_hash"}
        plan["state_hash"] = content_hash(because)
        # Same question, new search terms: a new inquiry and a new identity,
        # the way every new plan re-words the same ask.
        plan["inquiries"][0]["directed_document"]["query_terms"] = [
            "revenue recognition", because]
        plan["content_hash"] = content_hash(plan)
        stored = self.helper._record_plan(self.fixture, plan)
        return {**self.args, "plan_ref": stored["plan_id"],
                "inquiry_ref": inquiry_ref_for(inquiry_content_hash(plan["inquiries"][0]))}

    def _answered(self, admission_ref, at):
        connection = self.fixture.store.connection
        connection.execute(
            "CREATE TABLE IF NOT EXISTS mission_document_research_observations ("
            "observation_id TEXT PRIMARY KEY, admission_ref TEXT, admission_hash TEXT, "
            "mission_version_ref TEXT, company_ref TEXT, inquiry_ref TEXT, inquiry_hash TEXT, "
            "outcome TEXT, record_json TEXT, content_hash TEXT UNIQUE, created_at TEXT)")
        connection.execute(
            "INSERT INTO mission_document_research_observations VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            ("obs:" + admission_ref, admission_ref, "h", "m", "c", "i", "ih",
             "no_verified_claim", "{}", content_hash(admission_ref),
             at.isoformat(timespec="microseconds")))
        connection.commit()

    def test_a_rephrasing_waits_while_the_first_is_in_flight(self):
        with self.assertRaises(MissionDocumentResearchEquivalentPending) as caught:
            self.authority.admit_from_plan(**self._rephrased_args())
        self.assertEqual(caught.exception.equivalent["admission_ref"], self.first["id"])
        self.assertEqual(caught.exception.equivalent["state"], "in_flight")
        self.assertEqual(caught.exception.equivalent["matched_on"], "question")
        self.assertEqual(caught.exception.equivalent["rule_ref"],
                         "mission-document-research:equivalent-inquiry:v1")
        # The exact same inquiry is still just the duplicate it always was.
        self.assertEqual(self.authority.admit_from_plan(**self.args)["status_marker"],
                         "duplicate")

    def test_the_host_refuses_before_writing_a_backlog_question(self):
        from dalton_core.mission_document_research_admission import admit_directed_inquiry
        from dalton_core.research_question_backlog import ResearchQuestionBacklog

        args = self._rephrased_args()
        plan = json.loads(self.fixture.store.connection.execute(
            "SELECT plan_json FROM coverage_mission_research_plans WHERE plan_id=?",
            (args["plan_ref"],)).fetchone()[0])
        plan["plan_id"] = args["plan_ref"]
        registration = self.authority.registration_resolver(args["document_authority_ref"])
        count = self.fixture.store.connection.execute(
            "SELECT count(*) FROM backlog_question_versions").fetchone()[0]
        with self.assertRaises(MissionDocumentResearchEquivalentPending):
            admit_directed_inquiry(
                authority=self.authority, registrations={registration["id"]: registration},
                backlog=ResearchQuestionBacklog(self.fixture.store),
                mission=self.fixture.mission, plan=plan, inquiry=plan["inquiries"][0])
        self.assertEqual(self.fixture.store.connection.execute(
            "SELECT count(*) FROM backlog_question_versions").fetchone()[0], count)

    def test_a_withdrawn_equivalent_never_blocks(self):
        MissionDocumentWithdrawalAuthority(self.fixture.store).withdraw(
            self.first["id"], actor_ref="human:owner", reason="hopeless")
        admitted = self.authority.admit_from_plan(**self._rephrased_args())
        self.assertEqual(admitted["status_marker"], "fresh")

    def test_an_answer_blocks_for_the_cooldown_and_then_not(self):
        now = self.fixture.harness.clock.value
        self._answered(self.first["id"], now - timedelta(hours=1))
        with self.assertRaises(MissionDocumentResearchEquivalentPending) as caught:
            self.authority.admit_from_plan(**self._rephrased_args())
        self.assertEqual(caught.exception.equivalent["state"], "recently_answered")
        self.fixture.harness.clock.value = now + ANSWERED_COOLDOWN
        admitted = self.authority.admit_from_plan(**self._rephrased_args(because="later"))
        self.assertEqual(admitted["status_marker"], "fresh")


# -- the lane --------------------------------------------------------------------

def _withdrawal_row(connection, admission, *, actor="human:owner"):
    connection.executescript(
        "CREATE TABLE IF NOT EXISTS mission_document_research_withdrawals ("
        "withdrawal_id TEXT PRIMARY KEY, admission_ref TEXT NOT NULL UNIQUE, "
        "admission_hash TEXT NOT NULL, actor_ref TEXT NOT NULL, reason TEXT NOT NULL, "
        "record_json TEXT NOT NULL, content_hash TEXT NOT NULL UNIQUE, "
        "created_at TEXT NOT NULL);")
    body = {"schema_version": "0.1", "admission_ref": admission["id"],
            "admission_hash": admission["content_hash"], "actor_ref": actor,
            "reason": "hopeless", "hold": None,
            "created_at": "2026-09-30T00:00:00.000000+00:00"}
    wire = {**body, "id": "mission-document-research-withdrawal:" + content_hash(body)[:32]}
    wire["content_hash"] = content_hash(wire)
    connection.execute(
        "INSERT INTO mission_document_research_withdrawals VALUES(?,?,?,?,?,?,?,?)",
        (wire["id"], wire["admission_ref"], wire["admission_hash"], wire["actor_ref"],
         wire["reason"], canonical_json(wire), wire["content_hash"], wire["created_at"]))
    connection.commit()
    return wire


class LaneTests(unittest.TestCase):
    def setUp(self):
        from dalton_core.mission_document_research_lane import (
            MissionDocumentResearchCoordinator,
        )
        from tests.test_mission_document_research_lane import _Launcher, _Store

        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.store = _Store()
        self.addCleanup(self.store.connection.close)
        self.launcher = _Launcher(Path(self.temp.name) / "tickets")
        self.lane = MissionDocumentResearchCoordinator(store=self.store, launcher=self.launcher)

    def test_a_withdrawn_hold_is_terminal_and_waits_on_nobody(self):
        from dalton_core.mission_document_research_lane import _read_holds

        held = self.store.add(1)
        fresh = self.store.add(2)
        self.store.started(held["id"])
        holds = {}
        self.lane._hold(holds, held, reason="contract_failed_after_automatic_retry",
                        ticket_ref=None, disposition="recovery_required")
        _withdrawal_row(self.store.connection, held)
        result = self.lane.dispatch_once()
        # The one slot goes to the independent admission, never the withdrawn one.
        self.assertEqual(result["status"], "launched")
        self.assertEqual(self.launcher.started, [(fresh["id"], fresh["content_hash"])])
        stored = _read_holds(self.lane.holds_path)[held["id"]]
        self.assertEqual((stored["disposition"], stored["reason"]),
                         ("terminal_hold", WITHDRAWN_REASON))
        self.launcher.tickets[result["ticket_ref"]]["status"] = "succeeded"
        self.launcher.tickets[result["ticket_ref"]]["summary"] = {"status": "complete"}
        self.store.completed(fresh["id"])
        idle = self.lane.dispatch_once()
        self.assertEqual(idle["waiting_on_owner"], 0)
        self.assertEqual(idle["status"], "idle")

    def test_the_owner_ledger_no_longer_lists_it(self):
        from dalton_core.needs_human import lane_hold_ledgers

        held = self.store.add(1)
        self.store.started(held["id"])
        self.lane._hold({}, held, reason="unproved_send_failed_after_automatic_retry",
                        ticket_ref=None, disposition="recovery_required")
        state = self.launcher.tickets_dir.parent
        (state / "mission-document-research-runs").symlink_to(self.launcher.tickets_dir)
        before = lane_hold_ledgers(state)["mission_document_research"]
        self.assertEqual(len(before["escalated"]), 1)
        _withdrawal_row(self.store.connection, held)
        self.lane.dispatch_once()
        after = lane_hold_ledgers(state)["mission_document_research"]
        self.assertEqual(after["escalated"], [])
        self.assertEqual(after["waiting"], [])


class WriterDoorTests(unittest.TestCase):
    def setUp(self):
        self.fixtures, self.helper = _document_fixture(self)
        (self.fixture, self.authority, self.args,
         _registration, _launcher) = self.helper._fixture()
        self.admission = self.authority.admit_from_plan(**self.args)
        self.tickets = Path(self.fixture.state) / "withdraw-door-tickets"
        self.tickets.mkdir()
        self.statuses: dict[str, str] = {}
        launcher = SimpleNamespace(
            tickets_dir=self.tickets,
            status=lambda ref: {"id": ref, "status": self.statuses[ref]})
        self.server = SimpleNamespace(
            store=self.fixture.store, lane_launcher=lambda _kwarg: launcher)

    def _hold(self, **held):
        from dalton_core.mission_document_research_lane import HOLDS_FILE, _write_holds

        _write_holds(self.tickets / HOLDS_FILE, {self.admission["id"]: {
            "admission_hash": self.admission["content_hash"], "ticket_ref": None,
            "retry_at": None, **held}})

    def _withdraw(self, actor="human:owner"):
        from dalton_core.mission_document_research_lane import withdraw_admission

        return withdraw_admission(self.server, {
            "admission_ref": self.admission["id"], "actor_ref": actor,
            "reason": "the owner door was already used"})

    def test_refuses_a_timed_wait_but_closes_any_hold_waiting_on_a_person(self):
        # 2026-10-09: withdrawal is always one of the owner's doors on a
        # ``recovery_required`` hold, whatever the reason (available_doors).
        self._hold(disposition="recovery_wait", reason="scheduler_backoff",
                   retry_at="2026-09-30T00:00:00+00:00")
        self.assertEqual(self._withdraw()["reasons"], ["hold:lane_is_waiting_on_a_timer"])
        self.assertEqual(read_withdrawals(self.fixture.store.connection), {})
        self._hold(disposition="recovery_required",
                   reason="fresh_work_recovery_exhausted:route_rejected_definitely_not_sent")
        closed = self._withdraw()
        self.assertNotEqual(closed.get("status"), "refused")
        self.assertIn(self.admission["id"], read_withdrawals(self.fixture.store.connection))

    def test_refuses_while_a_child_or_a_work_order_runs(self):
        ticket = "mission-document-research:" + "a" * 24
        self._hold(disposition="terminal_hold", reason="failed", ticket_ref=ticket)
        self.statuses[ticket] = "running"
        self.assertEqual(self._withdraw()["reasons"], ["child:running:" + ticket])
        self.statuses[ticket] = "failed"
        with mock.patch("dalton_core.mission_document_research_withdrawal."
                        "running_work_orders", return_value=["work:x"]):
            self.assertEqual(self._withdraw()["reasons"], ["work:running:work:x"])
        with self.assertRaisesRegex(Exception, "human actor"):
            self._withdraw(actor="automation:lane")

    def test_withdraws_an_escalated_hold_and_turns_it_terminal(self):
        from dalton_core.mission_document_research_lane import HOLDS_FILE, _read_holds

        self._hold(disposition="recovery_required",
                   reason="contract_failed_after_automatic_retry")
        result = self._withdraw()
        self.assertEqual(result["status"], "withdrawn")
        self.assertEqual(result["actor_ref"], "human:owner")
        held = _read_holds(self.tickets / HOLDS_FILE)[self.admission["id"]]
        self.assertEqual((held["disposition"], held["reason"]),
                         ("terminal_hold", WITHDRAWN_REASON))
        self.assertEqual(self._withdraw()["status"], "already_withdrawn")

    def test_a_leased_work_order_counts_as_running(self):
        executor, _draft, _verifier = self.helper._executor(self.fixture, self.authority)
        executor.run_once(self.admission["id"])            # enqueue retrieval
        from dalton_core.mission_document_research_executor import _blueprints

        work = _blueprints(self.admission)[0]
        executor.scheduler.claim(executor.actor_ref, work_order_id=work["id"],
                                 lease_seconds=60)
        now = self.fixture.harness.clock.value
        self.assertEqual(running_work_orders(
            executor.scheduler.connection, self.admission["id"], now=now), [work["id"]])
        self.assertEqual(running_work_orders(
            executor.scheduler.connection, self.admission["id"],
            now=now + timedelta(minutes=5)), [])


class HoldRuleTests(unittest.TestCase):
    def test_which_holds_the_owner_may_close(self):
        cases = (
            ({"disposition": "terminal_hold", "reason": "failed"}, True),
            ({"disposition": "recovery_required",
              "reason": "reentry_failed_after_automatic_rebind:x"}, True),
            ({"disposition": "recovery_required",
              "reason": "provider_budget_exceeded_not_retried"}, True),
            ({"disposition": "recovery_required",
              "reason": "controlled_reentry_unavailable:x"}, True),   # 2026-10-09: any person-held reason
            ({"disposition": "recovery_required",
              "reason": "fresh_work_recovery_exhausted:all_models_unavailable"}, True),
            ({"disposition": "recovery_wait", "reason": "x"}, False),
            ({"disposition": "terminal_hold", "reason": WITHDRAWN_REASON}, False),
            (None, False),
        )
        for held, expected in cases:
            with self.subTest(held=held):
                self.assertEqual(withdrawable_hold(held)[0], expected)


# -- the CLI (read-only classification) -------------------------------------------

class UnrecoverableTests(unittest.TestCase):
    """Built on a synthetic state directory; nothing here touches a live one."""

    def setUp(self):
        from dalton_core.mission_document_research_lane import HOLDS_FILE, _write_holds

        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name)
        self.runs = self.state / "mission-document-research-runs"
        self.runs.mkdir()
        core = sqlite3.connect(self.state / "core.sqlite")
        core.executescript("""
            CREATE TABLE mission_document_research_admissions (
              admission_id TEXT PRIMARY KEY, record_json TEXT NOT NULL);
            CREATE TABLE mission_document_research_observations (
              observation_id TEXT PRIMARY KEY, admission_ref TEXT, outcome TEXT,
              record_json TEXT, created_at TEXT);
            CREATE TABLE mission_document_research_controlled_recovery_authorizations (
              authorization_id TEXT PRIMARY KEY, admission_ref TEXT NOT NULL,
              stage_ordinal INTEGER NOT NULL, failed_work_order_ref TEXT NOT NULL,
              record_json TEXT NOT NULL, content_hash TEXT NOT NULL, created_at TEXT NOT NULL);
            CREATE TABLE mission_document_research_recovery_links (
              recovery_link_id TEXT PRIMARY KEY, admission_ref TEXT NOT NULL,
              stage_ordinal INTEGER NOT NULL, recovery_number INTEGER NOT NULL,
              failed_work_order_ref TEXT NOT NULL, recovery_work_order_ref TEXT NOT NULL,
              record_json TEXT NOT NULL, content_hash TEXT NOT NULL, created_at TEXT NOT NULL);
            CREATE TABLE mission_document_research_model_authority_epoch_rebinds (
              rebind_id TEXT PRIMARY KEY, admission_ref TEXT NOT NULL,
              stage_ordinal INTEGER NOT NULL, recovery_link_ref TEXT NOT NULL,
              authorized_recovery_work_ref TEXT NOT NULL, current_base_work_ref TEXT NOT NULL,
              rebound_work_order_ref TEXT NOT NULL, record_json TEXT NOT NULL,
              content_hash TEXT NOT NULL, created_at TEXT NOT NULL);
            CREATE TABLE scheduler_formal_results (
              result_record_id TEXT PRIMARY KEY, work_order_id TEXT NOT NULL UNIQUE,
              terminal_state TEXT NOT NULL, created_at TEXT NOT NULL);
        """)
        self.refs = {name: f"mission-document-research-admission:{index:032x}"
                     for index, name in enumerate(
                         ("door", "input", "glm", "verifier", "fixable", "timer"), 1)}
        stages = {"door": "qualitative_model_draft", "input": "qualitative_model_draft",
                  "glm": "qualitative_model_draft",
                  "verifier": "independent_qualitative_verifier",
                  "fixable": "qualitative_model_draft"}
        for name, ref in self.refs.items():
            core.execute("INSERT INTO mission_document_research_admissions VALUES(?,?)",
                         (ref, json.dumps({"model_execution": {
                             "draft": {"max_output_tokens": 4096}}})))
            if name in stages:
                core.execute(
                    "INSERT INTO mission_document_research_observations VALUES(?,?,?,?,?)",
                    ("obs:" + name, ref, "recovery_required",
                     json.dumps({"stage": stages[name]}), "2026-09-29T00:00:00+00:00"))
        core.execute(
            "INSERT INTO mission_document_research_controlled_recovery_authorizations "
            "VALUES(?,?,?,?,?,?,?)",
            ("mission-document-unproved-send-recovery-authorization:1", self.refs["door"],
             2, "work:x", "{}", "h", "2026-09-26T00:00:00+00:00"))
        # The recovery that door bought ran and failed: that is what "used" means.
        core.execute(
            "INSERT INTO mission_document_research_recovery_links VALUES(?,?,?,?,?,?,?,?,?)",
            ("link:door", self.refs["door"], 2, 2, "work:x", "work:recovery-door",
             "{}", "h-link", "2026-09-26T00:00:00+00:00"))
        core.execute("INSERT INTO scheduler_formal_results VALUES(?,?,?,?)",
                     ("result:door", "work:recovery-door", "failed",
                      "2026-09-26T01:00:00+00:00"))
        core.commit()
        core.close()
        _write_holds(self.runs / HOLDS_FILE, {
            self.refs["door"]: self._held("unproved_send_failed_after_automatic_retry"),
            self.refs["input"]: self._held(
                "provider_budget_exceeded_after_corrected_retry"
                ":input=81500>81000:need=81000"),
            self.refs["glm"]: self._held("unproved_send_failed_after_automatic_retry"),
            self.refs["verifier"]: self._held("contract_failed_after_automatic_retry"),
            self.refs["fixable"]: self._held("contract_failed_after_automatic_retry"),
            self.refs["timer"]: {**self._held("scheduler_backoff"),
                                 "disposition": "recovery_wait",
                                 "retry_at": "2026-10-01T00:00:00+00:00"},
        })
        self._summary("glm", "qualitative_model_draft",
                      {"status": "failed", "accounting": {"usage": {
                          "output_tokens": 4096, "model": "glm-5.3"}},
                       "result": {"error": {"code": "MODEL_OUTPUT_CONTRACT_REJECTED"}}})
        self._summary("verifier", "independent_qualitative_verifier",
                      {"status": "failed", "result": {"metadata": {"rejected_model_output": {
                          "error_message": "passing verifier must preserve the exact "
                                           "candidate statement and have no error"}}}})
        self._summary("fixable", "qualitative_model_draft",
                      {"status": "failed", "result": {"metadata": {"rejected_model_output": {
                          "error_message": "directed-document draft is not strict JSON"}}}})

    def _held(self, reason):
        return {"admission_hash": "a" * 64, "ticket_ref": None, "reason": reason,
                "disposition": "recovery_required", "retry_at": None}

    def _summary(self, name, stage, outcome):
        directory = self.runs / content_hash(name)[:24]
        directory.mkdir()
        (directory / "summary.json").write_text(json.dumps({
            "admission_ref": self.refs[name], "status": "blocked",
            "outcomes": [{"stage": stage, **outcome}]}), encoding="utf-8")

    def test_each_class_and_nothing_else(self):
        from dalton_core import document_recovery_cli as cli

        with mock.patch.object(cli, "_current_draft_output_cap", return_value=16000):
            found = {item["admission_ref"]: item["class"]
                     for item in cli.unrecoverable_holds(self.state)}
        self.assertEqual(found, {
            self.refs["door"]: "owner_door_already_used",
            self.refs["input"]: "input_over_admitted_budget",
            self.refs["glm"]: "output_cap_exhausted_below_current_budget",
            self.refs["verifier"]: "verifier_restatement_under_retired_contract",
        })
        input_item = next(item for item in cli.unrecoverable_holds(self.state)
                          if item["admission_ref"] == self.refs["input"])
        self.assertEqual(input_item["evidence"]["provider_input_tokens"], 81500)
        self.assertEqual(input_item["evidence"]["admitted_max_input_tokens"], 81000)
        self.assertEqual(input_item["evidence"]["needed_max_input_tokens"], 81000)
        # Without a larger current cap the truncation is no argument for closing.
        with mock.patch.object(cli, "_current_draft_output_cap", return_value=4096):
            found = {item["class"] for item in cli.unrecoverable_holds(self.state)}
        self.assertNotIn("output_cap_exhausted_below_current_budget", found)

    def test_a_pre_correction_budget_hold_is_the_lanes_not_hopeless(self):
        """2026-10-06: the lane now corrects the ceiling once by itself."""

        from dalton_core import document_recovery_cli as cli
        from dalton_core.mission_document_research_lane import HOLDS_FILE, _write_holds

        _write_holds(self.runs / HOLDS_FILE, {
            self.refs["input"]: self._held("provider_budget_exceeded_not_retried")})
        self.assertEqual(cli.unrecoverable_holds(self.state), [])
        self.assertIsNone(cli.door_for("provider_budget_exceeded_not_retried"))
        self.assertIsNone(cli.door_for(
            "provider_budget_exceeded_after_corrected_retry:input=1>0"))

    def test_dry_run_writes_nothing_and_holds_suggests_it(self):
        import contextlib
        import io

        from dalton_core import document_recovery_cli as cli

        before = {path: path.read_bytes() for path in self.state.rglob("*") if path.is_file()}
        out = io.StringIO()
        with mock.patch.object(cli, "_current_draft_output_cap", return_value=16000), \
                mock.patch.object(cli, "_ephemeral_call") as call, \
                contextlib.redirect_stdout(out):
            self.assertEqual(cli.main(["withdraw-all-unrecoverable",
                                       "--state-dir", str(self.state)]), 0)
            view = cli.holds(self.state)["mission_document_research"]
        call.assert_not_called()
        after = {path: path.read_bytes() for path in self.state.rglob("*") if path.is_file()}
        self.assertEqual(before, after)
        summary = json.loads(out.getvalue()[out.getvalue().rindex("\n{"):])
        self.assertEqual(summary["results"], {"dry_run": 4})
        self.assertFalse(summary["applied"])
        self.assertEqual(view["unrecoverable"], 4)
        self.assertIn("withdraw-all-unrecoverable", view["suggest"])

    def test_apply_calls_the_writer_once_per_hold_with_a_human_actor(self):
        import contextlib
        import io

        from dalton_core import document_recovery_cli as cli

        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            cli.main(["withdraw-all-unrecoverable", "--state-dir", str(self.state),
                      "--apply"])
        with mock.patch.object(cli, "_current_draft_output_cap", return_value=16000), \
                mock.patch.object(cli, "_ephemeral_call",
                                  return_value={"status": "withdrawn"}) as call, \
                contextlib.redirect_stdout(io.StringIO()):
            cli.main(["withdraw-all-unrecoverable", "--state-dir", str(self.state),
                      "--apply", "--actor", "human:owner", "--only", self.refs["input"]])
        call.assert_called_once()
        params = call.call_args.kwargs["params"]
        self.assertEqual(params["admission_ref"], self.refs["input"])
        self.assertEqual(params["actor_ref"], "human:owner")
        self.assertTrue(params["reason"].startswith("unrecoverable:input_over_admitted_budget"))
        self.assertEqual(call.call_args.kwargs["operation"],
                         "withdraw_mission_document_admission")

    def test_withdraw_refuses_a_hold_the_lane_still_owns(self):
        import contextlib
        import io

        from dalton_core import document_recovery_cli as cli

        with mock.patch.object(cli, "_ephemeral_call") as call, \
                contextlib.redirect_stdout(io.StringIO()), \
                contextlib.redirect_stderr(io.StringIO()):
            code = cli.main(["withdraw", "--state-dir", str(self.state), "--admission-ref",
                             self.refs["timer"], "--reason", "x", "--apply",
                             "--actor", "human:owner"])
        self.assertEqual(code, 1)
        call.assert_not_called()

    # -- 2026-09-30: an owner recovery bought minutes ago is not a used door ------

    def _authorize(self, name, prefix, *, stage=2, at="2026-09-30T13:35:04+00:00"):
        core = sqlite3.connect(self.state / "core.sqlite")
        authorization = f"{prefix}:{name}"
        core.execute(
            "INSERT INTO mission_document_research_controlled_recovery_authorizations "
            "VALUES(?,?,?,?,?,?,?)",
            (authorization, self.refs[name], stage, "work:failed-" + name, "{}",
             "h-" + authorization, at))
        core.commit()
        core.close()
        return authorization

    def _dispatch(self, name, *, stage=2):
        core = sqlite3.connect(self.state / "core.sqlite")
        core.execute(
            "INSERT INTO mission_document_research_recovery_links VALUES(?,?,?,?,?,?,?,?,?)",
            ("link:" + name, self.refs[name], stage, 2, "work:failed-" + name,
             "work:recovery-" + name, "{}", "h-link-" + name, "2026-09-30T13:35:04+00:00"))
        core.commit()
        core.close()

    def _finish(self, work_ref, terminal_state):
        core = sqlite3.connect(self.state / "core.sqlite")
        core.execute("INSERT INTO scheduler_formal_results VALUES(?,?,?,?)",
                     ("result:" + work_ref, work_ref, terminal_state,
                      "2026-09-30T14:00:00+00:00"))
        core.commit()
        core.close()

    def _classified(self):
        from dalton_core import document_recovery_cli as cli

        skipped: list = []
        with mock.patch.object(cli, "_current_draft_output_cap", return_value=16000):
            found = {item["admission_ref"]: item
                     for item in cli.unrecoverable_holds(self.state, skipped=skipped)}
        return found, {item["admission_ref"]: item for item in skipped}

    def _cli_withdraw(self, ref):
        import contextlib
        import io

        from dalton_core import document_recovery_cli as cli

        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(cli, "_ephemeral_call",
                               return_value={"status": "withdrawn"}) as call, \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(["withdraw", "--state-dir", str(self.state), "--admission-ref",
                             ref, "--reason", "x", "--apply", "--actor", "human:owner"])
        return code, call, err.getvalue()

    def test_a_paid_recovery_bought_before_the_lane_tick_is_not_a_used_door(self):
        """Live legacy 3013db85: authorized 13:35:04Z, withdrawn minutes later."""

        from dalton_core import document_recovery_cli as cli

        ref = self.refs["fixable"]
        authorization = self._authorize(
            "fixable", "mission-document-paid-recovery-authorization")
        # Authorized, not yet dispatched: the hold still shows the failure the
        # owner just paid to retry.
        found, skipped = self._classified()
        self.assertNotIn(ref, found)
        self.assertEqual(skipped[ref]["reason"], cli.PENDING_OWNER_RECOVERY)
        self.assertEqual(skipped[ref]["pending"][0]["authorization_ref"], authorization)
        self.assertEqual(skipped[ref]["pending"][0]["state"], "not_dispatched")
        code, call, err = self._cli_withdraw(ref)
        self.assertEqual(code, 1)
        call.assert_not_called()
        self.assertIn("owner_recovery:pending:" + authorization, err)
        # Dispatched, no result yet: still pending.
        self._dispatch("fixable")
        found, skipped = self._classified()
        self.assertNotIn(ref, found)
        self.assertEqual(skipped[ref]["pending"][0]["state"], "pending")
        self.assertEqual(self._cli_withdraw(ref)[0], 1)
        # The recovery ran and failed: now the door is used.
        self._finish("work:recovery-fixable", "failed")
        found, skipped = self._classified()
        self.assertNotIn(ref, skipped)
        self.assertEqual(found[ref]["class"], "owner_door_already_used")
        self.assertEqual(found[ref]["evidence"]["authorization_ref"], authorization)
        self.assertEqual(found[ref]["evidence"]["recovery_state"], "failed")
        code, call, _err = self._cli_withdraw(ref)
        self.assertEqual(code, 0)
        call.assert_called_once()

    def test_a_rebound_recovery_counts_only_once_the_rebound_work_fails(self):
        ref = self.refs["fixable"]
        self._authorize("fixable", "mission-document-paid-recovery-authorization")
        self._dispatch("fixable")
        core = sqlite3.connect(self.state / "core.sqlite")
        core.execute(
            "INSERT INTO mission_document_research_model_authority_epoch_rebinds "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            ("rebind:fixable", ref, 2, "link:fixable", "work:recovery-fixable",
             "work:base", "work:rebound-fixable", "{}", "h-rebind",
             "2026-09-30T13:35:04+00:00"))
        core.commit()
        core.close()
        found, skipped = self._classified()
        self.assertNotIn(ref, found)
        self.assertIn(ref, skipped)
        self._finish("work:rebound-fixable", "failed")
        found, skipped = self._classified()
        self.assertEqual(found[ref]["class"], "owner_door_already_used")

    def test_a_recovery_that_succeeded_leaves_a_stale_hold_not_a_hopeless_one(self):
        ref = self.refs["fixable"]
        self._authorize("fixable", "mission-document-paid-recovery-authorization")
        self._dispatch("fixable")
        self._finish("work:recovery-fixable", "succeeded")
        found, skipped = self._classified()
        self.assertNotIn(ref, found)
        self.assertNotIn(ref, skipped)

    def test_a_door_used_on_another_stage_is_not_this_stages_door(self):
        # The owner bought a verifier retry that failed; the draft's paid door
        # is still unused, so a draft escalation is not "door already used".
        from dalton_core.mission_document_research_lane import HOLDS_FILE, _write_holds

        holds = {self.refs["fixable"]: self._held("contract_failed_after_automatic_retry")}
        _write_holds(self.runs / HOLDS_FILE, holds)
        core = sqlite3.connect(self.state / "core.sqlite")
        core.execute(
            "INSERT INTO mission_document_research_controlled_recovery_authorizations "
            "VALUES(?,?,?,?,?,?,?)",
            ("mission-document-paid-recovery-authorization:verifier", self.refs["fixable"], 3,
             "work:failed-verifier", "{}", "h-verifier", "2026-09-29T00:00:00+00:00"))
        core.execute(
            "INSERT INTO mission_document_research_recovery_links VALUES(?,?,?,?,?,?,?,?,?)",
            ("link:verifier", self.refs["fixable"], 3, 1, "work:failed-verifier",
             "work:recovery-verifier", "{}", "h-link-verifier", "2026-09-29T00:00:00+00:00"))
        core.execute("INSERT INTO scheduler_formal_results VALUES(?,?,?,?)",
                     ("result:verifier", "work:recovery-verifier", "failed",
                      "2026-09-29T01:00:00+00:00"))
        core.commit()
        core.close()
        # ``fixable`` escalated on the draft (ordinal 2); its only owner door
        # was used on ordinal 3.
        found, _skipped = self._classified()
        self.assertNotIn(self.refs["fixable"], found)

    def test_the_guard_covers_every_class_and_the_reentry_door(self):
        from types import SimpleNamespace

        from dalton_core.lane_reentry_claim import grant_path

        # ``input`` would be input_over_admitted_budget; ``glm`` an exhausted
        # output cap; ``verifier`` the retired verifier contract.  Each has an
        # owner decision the lane has not yet run, so none is listed.
        self._authorize("input", "mission-document-unproved-send-recovery-authorization")
        self._authorize("glm", "mission-document-unproved-send-recovery-authorization")
        self._dispatch("glm")
        path = grant_path(SimpleNamespace(tickets_dir=self.runs), self.refs["verifier"])
        path.write_text(json.dumps({
            "kind": "owner_authorized_controlled_reentry",
            "admission_ref": self.refs["verifier"], "actor_ref": "human:owner",
            "granted_at": "2026-09-30T13:35:04+00:00", "max_reentries": 1}),
            encoding="utf-8")
        found, skipped = self._classified()
        self.assertEqual(set(found), {self.refs["door"]})
        self.assertEqual(set(skipped), {self.refs[name]
                                        for name in ("input", "glm", "verifier")})
        self.assertTrue(all(item["reason"] == "pending owner recovery"
                            for item in skipped.values()))
        code, _call, err = self._cli_withdraw(self.refs["verifier"])
        self.assertEqual(code, 1)
        self.assertIn("owner_reentry:pending:", err)

    def test_the_dry_run_says_why_a_pending_hold_is_absent(self):
        import contextlib
        import io

        from dalton_core import document_recovery_cli as cli

        self._authorize("fixable", "mission-document-paid-recovery-authorization")
        out = io.StringIO()
        with mock.patch.object(cli, "_current_draft_output_cap", return_value=16000), \
                contextlib.redirect_stdout(out):
            cli.main(["withdraw-all-unrecoverable", "--state-dir", str(self.state)])
        text = out.getvalue()
        self.assertIn('"reason": "pending owner recovery"', text)
        summary = json.loads(text[text.rindex("\n{"):])
        self.assertEqual(summary["skipped_pending_owner_recovery"], 1)
        self.assertEqual(summary["results"], {"dry_run": 4})


class OwnerRecoveryRaceTests(unittest.TestCase):
    """End to end on a real executor: authorize-paid, then withdraw before the tick."""

    def test_withdraw_waits_for_the_recovery_the_owner_just_bought(self):
        from dalton_core import document_recovery_cli as cli
        from dalton_core.mission_document_research_lane import (
            HOLDS_FILE, _write_holds, withdraw_admission,
        )
        from dalton_core.mission_document_research_withdrawal import owner_recoveries

        fixtures, helper = _document_fixture(self)
        fixture, _authority, executor, admission, granted = helper._owner_door(
            fixtures.CountingFakeAdapter({"schema_version": "0.1", "status": "answered"}),
            door="paid", reason="contract_failed_after_automatic_retry")
        runs = Path(fixture.state) / "mission-document-research-runs"
        runs.mkdir(exist_ok=True)
        # The lane has not ticked: its ledger still shows the escalation the
        # owner has just paid to retry.
        _write_holds(runs / HOLDS_FILE, {admission["id"]: {
            "admission_hash": admission["content_hash"], "ticket_ref": None,
            "retry_at": None, "disposition": "recovery_required",
            "reason": "contract_failed_after_automatic_retry"}})
        server = SimpleNamespace(
            store=fixture.store,
            lane_launcher=lambda _kwarg: SimpleNamespace(
                tickets_dir=runs, status=lambda ref: {"id": ref, "status": "failed"}))

        def classify():
            skipped: list = []
            with mock.patch.object(cli, "_current_draft_output_cap", return_value=None):
                found = {item["admission_ref"]: item for item in
                         cli.unrecoverable_holds(fixture.state, skipped=skipped)}
            return found, {item["admission_ref"]: item for item in skipped}

        def withdraw():
            return withdraw_admission(server, {
                "admission_ref": admission["id"], "actor_ref": "human:owner",
                "reason": "owner door already used"})

        found, skipped = classify()
        self.assertNotIn(admission["id"], found)
        self.assertEqual(skipped[admission["id"]]["reason"], "pending owner recovery")
        refused = withdraw()
        self.assertEqual(refused["status"], "refused")
        self.assertIn("owner_recovery:pending:" + granted["authorization_ref"],
                      refused["reasons"])
        self.assertEqual(read_withdrawals(fixture.store.connection), {})

        # The lane ticks: the recovery is dispatched and its reply fails the
        # contract again.
        for _ in range(20):
            executor.run_once(admission["id"])
            states = [item["state"] for item in owner_recoveries(
                fixture.store.connection, admission["id"])]
            if states == ["failed"]:
                break
        self.assertEqual(states, ["failed"])
        for reason in ("contract_failed_after_automatic_retry",       # stale ledger
                       "contract_failed_after_owner_authorized_retry"):  # after the tick
            with self.subTest(reason=reason):
                _write_holds(runs / HOLDS_FILE, {admission["id"]: {
                    "admission_hash": admission["content_hash"], "ticket_ref": None,
                    "retry_at": None, "disposition": "recovery_required",
                    "reason": reason}})
                found, skipped = classify()
                self.assertNotIn(admission["id"], skipped)
                self.assertEqual(found[admission["id"]]["class"], "owner_door_already_used")
                self.assertEqual(found[admission["id"]]["evidence"]["authorization_ref"],
                                 granted["authorization_ref"])
        self.assertEqual(withdraw()["status"], "withdrawn")


if __name__ == "__main__":
    unittest.main()
