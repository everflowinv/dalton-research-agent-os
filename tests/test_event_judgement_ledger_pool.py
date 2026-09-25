"""2026-09-25: the event_response pool is what the day ledger says it cost.

The lane's own ``event_response_spend`` book missed every call the lane never
heard back from (live 09-24: 38.2 USD booked against 55.3 USD settled), so the
50 USD cap was checked against the smaller number.  The pool is now read from
the day ledger -- every admission for the pool's purposes, settled or open --
with the same per-purpose reader the claim-support ceiling uses.
"""

from __future__ import annotations

import sqlite3
from datetime import timedelta
from pathlib import Path

from dalton_core.event_judgement import (
    POOL_LEDGER_PURPOSES,
    EventJudgementAuthority,
    pool,
    pool_state,
)
from dalton_core.event_judgement_cli import run_judgement, unjudged_event_groups
from dalton_core.research_event import ResearchEventAuthority, record_event
from dalton_core.tracking_cadence import POLICY_PATH
from tests.p14a_fixtures import ACN, AUTOMATION, P14aHarness
from tests.test_event_judgement import (
    JUDGE_ROUTE,
    PASS,
    VERIFIER_ROUTE,
    FakeModel,
    decision,
    resolver,
)
from tests.test_mission_event_judgement_lane import NOW

DAY = NOW.date().isoformat()

def make_ledger(path: Path, rows, *, corrections=()) -> Path:
    """The three day-ledger tables ``purpose_spend_micros`` reads."""

    connection = sqlite3.connect(path)
    connection.executescript("""
        CREATE TABLE IF NOT EXISTS thesis_impact_day_admissions (admission_id TEXT,
            day TEXT, work_order_ref TEXT, reserved_micros INTEGER);
        CREATE TABLE IF NOT EXISTS thesis_impact_day_settlements (admission_id TEXT,
            actual_micros INTEGER);
        CREATE TABLE IF NOT EXISTS thesis_impact_settlement_corrections (admission_id TEXT,
            corrected_micros INTEGER);
    """)
    for admission_id, day, work_order_ref, reserved, actual in rows:
        connection.execute(
            "INSERT INTO thesis_impact_day_admissions VALUES(?,?,?,?)",
            (admission_id, day, work_order_ref, reserved))
        if actual is not None:
            connection.execute(
                "INSERT INTO thesis_impact_day_settlements VALUES(?,?)",
                (admission_id, actual))
    connection.executemany(
        "INSERT INTO thesis_impact_settlement_corrections VALUES(?,?)", corrections)
    connection.commit()
    connection.close()
    return path


class LedgerModel(FakeModel):
    """A model whose every call is admitted into the day ledger at ``ledger_cost``
    while telling the lane it cost ``cost_micros`` -- the gap the lane's book fell
    into on 2026-09-23/24."""

    def __init__(self, replies, *, ledger: Path, ledger_cost: int, **kwargs):
        super().__init__(replies, **kwargs)
        self.ledger = ledger
        self.ledger_cost = ledger_cost

    def call(self, *, purpose, request_id, prompt, mission):
        make_ledger(self.ledger, [(
            f"a-{purpose}-{request_id}", DAY,
            f"work:cockpit-{purpose}-{request_id}", self.ledger_cost, self.ledger_cost)])
        return super().call(purpose=purpose, request_id=request_id, prompt=prompt,
                            mission=mission)


class Harness(P14aHarness):
    def setUp(self):
        super().setUp()
        self.events = ResearchEventAuthority(self.store)
        self.judgements = EventJudgementAuthority(self.store)
        self.pass_screen(ACN)
        self.cap = pool(self.mission)["cap_micros"]

    def document(self, kind, document, *, day=9, hour=10, discovery="d"):
        return record_event(
            self.events, company_ref=ACN, kind=kind,
            occurred_at=f"2026-09-{day:02d}T{hour:02d}:00:00+00:00",
            source_refs=["source:alphaengine", document],
            payload={"document_ref": document, "source_ref": "source:alphaengine",
                     "spec_ref": "sell-side-reports", "discovery_ref": discovery,
                     "title": None, "host": None},
            mission=self.mission, actor_ref=AUTOMATION,
        )

    def claim(self, claim_ref, statement, *, period="Q3 FY26", day=9, hour=10):
        version = "claim-version:" + claim_ref.rsplit(":", 1)[-1]
        return record_event(
            self.events, company_ref=ACN, kind="claim",
            occurred_at=f"2026-09-{day:02d}T{hour:02d}:00:00+00:00",
            source_refs=[version],
            payload={"claim_ref": claim_ref, "claim_version_ref": version,
                     "metric_ref": None, "period": period, "statement": statement,
                     "source_ref": None},
            mission=self.mission, actor_ref=AUTOMATION,
        )

    def groups(self, **kwargs):
        return unjudged_event_groups(self.events, self.judgements, company_ref=ACN,
                                     limit=None, now=NOW, **kwargs)

    def judge_run(self, *, judge=None, verifier=None, **kwargs):
        judge = judge or FakeModel([decision()] * 20, route=JUDGE_ROUTE)
        verifier = verifier or FakeModel([PASS] * 20, route=VERIFIER_ROUTE)
        summary = run_judgement(
            state_dir=self.state_dir, summary_dir=self.state_dir / "judge",
            policy_path=POLICY_PATH, judge_model=judge, verifier_model=verifier,
            family_resolver=resolver(), **{"now": NOW, **kwargs},
        )
        return summary, judge, verifier


class DayLedgerPoolTests(Harness):
    def test_the_pool_is_the_day_ledger_not_the_lane_book(self):
        ledger = make_ledger(self.state_dir / "budget.sqlite", [
            # judged, and booked by the lane too
            ("a1", DAY, "work:cockpit-event_judgement-1", 1_000_000, 280_000),
            ("a2", DAY, "work:cockpit-event_judgement_verifier-1", 1_000_000, 3_000),
            # "this request is already running": never booked, settled at actual
            ("a3", DAY, "work:cockpit-event_judgement-2", 116_140, 282_746),
            # an overrun the settlement later corrected
            ("a4", DAY, "work:cockpit-event_judgement-3", 451_680, 27_420),
            # still open: counts at what it holds
            ("a5", DAY, "work:cockpit-event_judgement-4", 451_392, None),
            ("a6", DAY, "work:cockpit-thesis_reflection-1", 1_000_000, 50_000),
            ("a7", DAY, "work:cockpit-earnings_preview-1", 1_000_000, 70_000),
            # not this pool, and not this day
            ("a8", DAY, "work:cockpit-claim_support_verifier-1", 1_000_000, 90_000),
            ("a9", "2026-09-08", "work:cockpit-event_judgement-5", 1_000_000, 900_000),
        ], corrections=[("a4", 279_842)])
        self.judgements.record_spend(
            call={"work_order_ref": "work:cockpit-event_judgement-1", "cost_micros": 280_000},
            purpose="event_judgement", outcome="judged", event_ref="research-event:x",
            mission=self.mission, day=DAY)
        self.assertEqual(pool_state(self.judgements, self.mission, day=DAY)["spent_micros"],
                         280_000)
        state = pool_state(self.judgements, self.mission, day=DAY, budget_db=ledger)
        expected = 280_000 + 3_000 + 282_746 + 279_842 + 451_392 + 50_000 + 70_000
        self.assertEqual(state["source"], "day_ledger")
        self.assertEqual(state["spent_micros"], expected)
        self.assertEqual(state["remaining_micros"], max(0, self.cap - expected))
        self.assertEqual(state["spent_by_purpose"]["event_judgement_verifier"], 3_000)

    def test_a_refusal_never_sent_does_not_fill_the_pool(self):
        # The pool reads purpose_spend_micros, so a call the adapter refused
        # over contract wiring before sending it -- settled at its reservation
        # with no usage entry before 4fa1e3c4 -- is not spend here either.
        import json

        ledger = self.state_dir / "ledger" / "thesis-impact-budget.sqlite"
        ledger.parent.mkdir()
        connection = sqlite3.connect(ledger)
        connection.executescript("""
            CREATE TABLE thesis_impact_day_admissions (admission_id TEXT, day TEXT,
                work_order_ref TEXT, attempt_number INTEGER, reserved_micros INTEGER);
            CREATE TABLE thesis_impact_day_settlements (admission_id TEXT,
                actual_micros INTEGER, usage_entry_ref TEXT);
        """)
        connection.executemany(
            "INSERT INTO thesis_impact_day_admissions VALUES(?,?,?,?,?)", [
                ("w", DAY, "work:cockpit-event_judgement_verifier-w", 1, 400_000),
                ("t", DAY, "work:cockpit-event_judgement-t", 1, 300_000)])
        connection.executemany(
            "INSERT INTO thesis_impact_day_settlements VALUES(?,?,?)",
            [("w", 400_000, None), ("t", 300_000, None)])
        connection.commit()
        connection.close()
        scheduler = self.state_dir / "sched.sqlite"
        connection = sqlite3.connect(scheduler)
        connection.execute(
            "CREATE TABLE scheduler_result_envelopes (work_order_id TEXT, "
            "attempt_number INTEGER, result_envelope_json TEXT, created_at TEXT)")
        for ref, message in (
                ("work:cockpit-event_judgement_verifier-w",
                 "the model call failed: independent verifier provider contract "
                 "is unsupported"),
                ("work:cockpit-event_judgement-t", "TIMEOUT")):
            connection.execute(
                "INSERT INTO scheduler_result_envelopes VALUES(?,?,?,?)",
                (ref, 1, json.dumps({"status": "failed", "metadata": {
                    "chain_failures": [{"code": "x", "message": message}]}}), DAY))
        connection.commit()
        connection.close()
        state = pool_state(self.judgements, self.mission, day=DAY, budget_db=ledger,
                           scheduler_db=scheduler)
        self.assertEqual(state["spent_micros"], 300_000)
        self.assertEqual(state["spent_by_purpose"]["event_judgement_verifier"], 0)
        # Without the scheduler nothing can be shown unsent: both count.
        self.assertEqual(
            pool_state(self.judgements, self.mission, day=DAY,
                       budget_db=ledger)["spent_micros"], 700_000)

    def test_the_pool_counts_every_purpose_that_books_into_it(self):
        from dalton_core import earnings_season, event_judgement

        for purpose in (event_judgement.PURPOSE, event_judgement.VERIFIER_PURPOSE,
                        event_judgement.REFLECTION_PURPOSE,
                        event_judgement.REFLECTION_VERIFIER_PURPOSE,
                        earnings_season.PREVIEW_PURPOSE,
                        earnings_season.PREVIEW_VERIFIER_PURPOSE,
                        earnings_season.CALIBRATION_PURPOSE,
                        earnings_season.CALIBRATION_VERIFIER_PURPOSE):
            self.assertIn(purpose, POOL_LEDGER_PURPOSES)

    def test_an_unreadable_ledger_has_no_room_and_pays_for_nothing(self):
        self.document("transcript", "alphaengine-doc:1")
        state = pool_state(self.judgements, self.mission, day=DAY,
                           budget_db=self.state_dir / "missing.sqlite")
        self.assertEqual(state["remaining_micros"], 0)
        self.assertIn("no day ledger", state["ledger_error"])
        summary, judge, _ = self.judge_run(budget_db=str(self.state_dir / "missing.sqlite"))
        self.assertEqual(summary["judgement_status"], "gated:ledger_unreadable")
        self.assertEqual(judge.prompts, [])

    def test_a_ledger_at_the_cap_stops_the_lane_and_the_next_day_resumes(self):
        self.document("transcript", "alphaengine-doc:1")
        # The lane's own book is empty: only the ledger knows the day is spent.
        ledger = make_ledger(self.state_dir / "budget.sqlite", [
            ("a1", DAY, "work:cockpit-event_judgement-unbooked", self.cap, self.cap - 1)])
        summary, judge, _ = self.judge_run(budget_db=str(ledger))
        self.assertEqual(summary["judgement_status"], "skipped:pool_exhausted")
        self.assertEqual(judge.prompts, [])
        self.assertEqual(self.judgements.judged_count(ACN), 0)
        tomorrow, judge, _ = self.judge_run(budget_db=str(ledger), now=NOW + timedelta(days=1))
        self.assertEqual(tomorrow["judgement_status"], "judged")
        self.assertEqual(tomorrow["pool"]["spent_micros"], 0)
        self.assertEqual(len(judge.prompts), 1)
        self.assertEqual(self.judgements.judged_count(ACN), 1)

    def test_the_ledger_is_reread_between_events_within_one_run(self):
        for day in (7, 8, 9):
            self.document("transcript", f"alphaengine-doc:{day}", day=day)
        ledger = make_ledger(self.state_dir / "budget.sqlite", [])
        # Each call reports nothing to the lane and settles at most of the cap.
        judge = LedgerModel([decision()] * 3, route=JUDGE_ROUTE, cost_micros=0,
                            ledger=ledger, ledger_cost=self.cap - 100_000)
        verifier = FakeModel([PASS] * 3, route=VERIFIER_ROUTE, cost_micros=0)
        summary, _, _ = self.judge_run(judge=judge, verifier=verifier, budget_db=str(ledger))
        self.assertEqual(summary["judged"], 1)
        self.assertEqual(summary["judgement_status"], "skipped:pool_exhausted")
        self.assertEqual(summary["cost_micros"], 0, "the book saw nothing")
        self.assertEqual(len(judge.prompts), 1)

    def test_the_budget_db_is_read_from_the_judge_configuration(self):
        import json

        from dalton_core.event_judgement_cli import _budget_db

        path = self.state_dir / "judge.json"
        path.write_text(json.dumps({"budget_db": "/x/budget.sqlite"}), encoding="utf-8")
        self.assertEqual(_budget_db(path, FakeModel([])), "/x/budget.sqlite")
        self.assertIsNone(_budget_db(None, FakeModel([])))


if __name__ == "__main__":
    import unittest

    unittest.main()
