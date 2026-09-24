"""A NO_CHANGE judgement is recorded, not published; its day closes into one digest.

Live (2026-09-23) ACN's event_note chain gained nine NO_CHANGE versions in four
hours, each a paraphrase of the last.  These pin the replacement: NO_CHANGE
notes wait, anything else publishes at once, and a closed UTC day yields at
most one digest version per company that lists every input and its conclusion.
"""

from __future__ import annotations

import json
import unittest
from datetime import date, datetime, timezone
from unittest import mock

from dalton_core.event_judgement import (
    DAILY_DIGEST_FIGURE_ELIDED,
    DAILY_DIGEST_MARKER,
    DAILY_DIGEST_TEMPLATE_REF,
    apply_effect,
    build_daily_digest,
    judge,
    publish_daily_digests,
    verify,
)
from dalton_core.mission_deliverable import MissionDeliverableAuthority
from dalton_core.mission_event_judgement_lane import (
    MissionEventJudgementLaneCoordinator,
    publish_no_change_digests,
)
from dalton_core.research_event import record_event
from tests.p14a_fixtures import ACN, AUTOMATION, CTSH
from tests.test_event_judgement import (
    PASS,
    VERIFIER_ROUTE,
    FakeModel,
    JudgementHarness,
    decision,
    resolver,
)

CHAIN = "mission-deliverable:event_note:0001467373"
DAY = "2026-09-23"


class DailyDigestHarness(JudgementHarness):
    def setUp(self):
        super().setUp()
        self.deliverables = MissionDeliverableAuthority(self.store)
        self._n = 0
        self._template = self.event

    def new_event(self, company_ref=ACN):
        self._n += 1
        return record_event(
            self.events, company_ref=company_ref, kind="price_move",
            occurred_at=f"2026-08-{self._n:02d}T00:00:00+00:00",
            source_refs=[f"market-price-series-version:{self._n}"],
            payload={**self._template["payload"],
                     "price_version_ref": f"market-price-series-version:{self._n}"},
            mission=self.mission, actor_ref=AUTOMATION,
        )

    def judged(self, event, body):
        self.event = event
        context = self.context()
        decided = judge(context, model=FakeModel([body]), mission=self.mission,
                        request_id=f"r{self._n}")
        self.assertEqual(decided["status"], "judged", decided.get("reason"))
        checked = verify(context, decided, model=FakeModel([PASS], route=VERIFIER_ROUTE),
                         mission=self.mission, request_id=f"v{self._n}",
                         family_resolver=resolver())
        return context, decided, checked

    def run_event(self, body, *, at, company_ref=ACN):
        """Judge a fresh event; ``EVENT`` in the citations stands for its id."""

        event = self.new_event(company_ref)
        body = dict(body)
        citations = [event["id"] if ref == "EVENT" else ref
                     for ref in body.get("citations") or ()]
        if body["action"] != "no_change" and not citations:
            citations = [event["id"]]
        body["citations"] = citations
        context, decided, checked = self.judged(event, body)
        effect = apply_effect(
            event=event, judgement=decided, context=context, mission=self.mission,
            playbook=self.playbook, deliverables=self.deliverables,
            forecast_models=None, model_version=None, research_admitter=None,
            actor_ref=AUTOMATION,
        )
        with mock.patch("dalton_core.event_judgement._now", return_value=at):
            written = self.judgements.record(
                event=event, judgement=decided, verification=checked, effect=effect,
                mission=self.mission, actor_ref=AUTOMATION,
            )
        return effect, written

    def versions(self, ref=CHAIN):
        return [
            json.loads(row["record_json"]) for row in self.store.connection.execute(
                "SELECT record_json FROM mission_deliverable_versions "
                "WHERE deliverable_ref=? ORDER BY version_number", (ref,)
            ).fetchall()
        ]

    def digest(self, today=date(2026, 9, 24)):
        return publish_daily_digests(
            self.deliverables, mission=self.mission, playbook=self.playbook,
            actor_ref=AUTOMATION, today=today,
        )


def no_change_note(note="板块整体波动，公司层面没有新消息，现行判断不变。", **extra):
    return decision(action="note", word="NO_CHANGE", note=note, **extra)


class DeferralTests(DailyDigestHarness):
    def test_a_no_change_note_is_recorded_but_not_published(self):
        effect, written = self.run_event(no_change_note(), at=f"{DAY}T09:00:00+00:00")
        self.assertEqual(effect["status"], "deferred")
        self.assertEqual(effect["daily_digest"], DAILY_DIGEST_MARKER)
        self.assertEqual(written["effect"]["status"], "deferred")
        self.assertEqual(written["note"], "板块整体波动，公司层面没有新消息，现行判断不变。")
        self.assertEqual(self.versions(), [])

    def test_every_no_change_carries_the_marker_and_nothing_else_does(self):
        effect, _ = self.run_event(decision(), at=f"{DAY}T09:00:00+00:00")
        self.assertEqual(effect["kind"], "no_change")
        self.assertEqual(effect["daily_digest"], DAILY_DIGEST_MARKER)
        claim = self.claim(statement="客户削减了支出。")
        effect, _ = self.run_event(decision(
            action="note", word="THESIS_WEAKENED", citations=["EVENT", claim],
            note="客户削减支出，削弱了需求论点。"), at=f"{DAY}T10:00:00+00:00")
        self.assertEqual(effect["status"], "fresh")
        self.assertNotIn("daily_digest", effect)
        self.assertEqual(len(self.versions()), 1)


class DigestTests(DailyDigestHarness):
    def test_nine_no_change_notes_in_a_day_become_one_digest_version(self):
        for hour in range(9, 18):
            self.run_event(no_change_note(note=f"第 {hour - 8} 次看这条卖方观点，判断不变。"),
                           at=f"{DAY}T{hour:02d}:00:00+00:00")
        self.assertEqual(self.versions(), [])
        results = self.digest()
        self.assertEqual([item["status"] for item in results], ["fresh"])
        versions = self.versions()
        self.assertEqual(len(versions), 1)
        digest = versions[0]
        self.assertEqual(digest["kind"], "event_note")
        self.assertEqual(digest["template_ref"], DAILY_DIGEST_TEMPLATE_REF)
        self.assertEqual(digest["idempotency_key"], f"event-note-daily-digest:{ACN}:{DAY}")
        # One header and one entry per input, in the order they were judged.
        self.assertEqual(len(digest["sections"]), 10)
        self.assertIn("考察输入 9 条", digest["sections"][0]["title"])
        self.assertIn("NO_CHANGE/note", digest["sections"][1]["title"])
        self.assertIn("判断不变", digest["sections"][1]["body"])

    def test_a_replay_and_a_second_tick_publish_nothing_more(self):
        self.run_event(no_change_note(), at=f"{DAY}T09:00:00+00:00")
        self.assertEqual(len(self.digest()), 1)
        self.assertEqual(self.digest(), [])
        self.assertEqual(self.digest(date(2026, 9, 25)), [])
        self.assertEqual(len(self.versions()), 1)

    def test_an_open_day_is_not_digested(self):
        self.run_event(no_change_note(), at=f"{DAY}T09:00:00+00:00")
        self.assertEqual(self.digest(date(2026, 9, 23)), [])
        self.assertEqual(self.versions(), [])

    def test_each_company_gets_its_own_digest(self):
        self.run_event(no_change_note(), at=f"{DAY}T09:00:00+00:00")
        self.run_event(decision(), at=f"{DAY}T11:00:00+00:00", company_ref=CTSH)
        results = self.digest()
        self.assertEqual(sorted(item["company_ref"] for item in results), sorted([ACN, CTSH]))
        self.assertEqual(len(self.versions()), 1)
        self.assertEqual(len(self.versions("mission-deliverable:event_note:0001058290")), 1)

    def test_days_before_the_marker_existed_are_not_backfilled(self):
        _effect, written = self.run_event(no_change_note(), at=f"{DAY}T09:00:00+00:00")
        # A pre-deploy row: same decision, no marker on its effect.
        row = dict(written)
        row["effect"] = {"kind": "note", "status": "fresh",
                         "deliverable_version_ref": "mission-deliverable-version:old"}
        self.assertIsNone(build_daily_digest(ACN, DAY, [row]))

    def test_a_day_with_an_immediate_note_lists_it_beside_the_no_changes(self):
        self.run_event(no_change_note(), at=f"{DAY}T09:00:00+00:00")
        claim = self.claim(statement="客户削减了支出。")
        self.run_event(decision(
            action="note", word="THESIS_WEAKENED", citations=["EVENT", claim],
            note="客户削减支出，削弱了需求论点。"), at=f"{DAY}T10:00:00+00:00")
        self.digest()
        versions = self.versions()
        self.assertEqual(len(versions), 2)
        digest = versions[-1]
        self.assertIn("NO_CHANGE 1 条", digest["sections"][0]["title"])
        self.assertIn("THESIS_WEAKENED/note", digest["sections"][2]["title"])
        self.assertEqual(digest["sections"][2]["body"], "结论已即时发布为独立事件笔记。")

    def test_a_day_of_only_immediate_notes_owes_no_digest(self):
        claim = self.claim(statement="客户削减了支出。")
        self.run_event(decision(
            action="note", word="THESIS_WEAKENED", citations=["EVENT", claim],
            note="客户削减支出，削弱了需求论点。"), at=f"{DAY}T10:00:00+00:00")
        self.assertEqual(self.digest(), [])
        self.assertEqual(len(self.versions()), 1)

    def test_an_unsourced_figure_is_elided_not_a_refusal_of_the_day(self):
        self.run_event(decision(because="收入季度增长 14.5% 与预测一致，无需改写。"),
                       at=f"{DAY}T09:00:00+00:00")
        results = self.digest()
        self.assertEqual(results[0]["status"], "fresh", results)
        body = self.versions()[0]["sections"][1]["body"]
        self.assertNotIn("14.5", body)
        self.assertIn(DAILY_DIGEST_FIGURE_ELIDED, body)

    def test_a_retired_claim_is_dropped_rather_than_refusing_the_digest(self):
        claim = self.claim(statement="管理层说需求在改善。")
        self.run_event(no_change_note(citations=["EVENT", claim]),
                       at=f"{DAY}T09:00:00+00:00")
        rows = [json.loads(row["record_json"]) for row in self.store.connection.execute(
            "SELECT record_json FROM event_judgements").fetchall()]
        kept = build_daily_digest(ACN, DAY, rows, live_claim_refs={claim})
        dropped = build_daily_digest(ACN, DAY, rows, live_claim_refs=set())
        self.assertEqual(kept["sections"][1]["claim_refs"], [claim])
        self.assertEqual(dropped["sections"][1]["claim_refs"], [])

    def test_the_render_is_deterministic(self):
        self.run_event(no_change_note(), at=f"{DAY}T09:00:00+00:00")
        self.run_event(decision(), at=f"{DAY}T09:00:00+00:00")
        rows = [json.loads(row["record_json"]) for row in self.store.connection.execute(
            "SELECT record_json FROM event_judgements").fetchall()]
        self.assertEqual(build_daily_digest(ACN, DAY, rows),
                         build_daily_digest(ACN, DAY, list(reversed(rows))))


class LaneTickTests(DailyDigestHarness):
    def coordinator(self, now, calls):
        def digest(mission, today):
            calls.append(today)
            return publish_no_change_digests(self.store, mission, today)

        return MissionEventJudgementLaneCoordinator(
            launcher=mock.Mock(), mission=lambda: self.mission,
            pending=lambda _mission: [], failure_ledger_dir=self.state_dir,
            digest=digest, clock=lambda: now[0],
        )

    def test_the_tick_digests_a_closed_day_once_even_with_nothing_to_judge(self):
        self.run_event(no_change_note(), at=f"{DAY}T09:00:00+00:00")
        now = [datetime(2026, 9, 23, 23, 0, tzinfo=timezone.utc)]
        calls: list[date] = []
        coordinator = self.coordinator(now, calls)
        first = coordinator.dispatch_once()
        self.assertEqual(first["status"], "idle")
        self.assertNotIn("digests", first)
        self.assertEqual(self.versions(), [])
        now[0] = datetime(2026, 9, 24, 0, 5, tzinfo=timezone.utc)
        second = coordinator.dispatch_once()
        self.assertEqual([item["status"] for item in second["digests"]], ["fresh"])
        coordinator.dispatch_once()
        self.assertEqual(calls, [date(2026, 9, 23), date(2026, 9, 24)])
        self.assertEqual(len(self.versions()), 1)

    def test_a_failing_digest_never_fails_the_tick_and_is_retried(self):
        now = [datetime(2026, 9, 24, 1, 0, tzinfo=timezone.utc)]
        attempts = []

        def digest(mission, today):
            attempts.append(today)
            raise RuntimeError("database is locked")

        coordinator = MissionEventJudgementLaneCoordinator(
            launcher=mock.Mock(), mission=lambda: self.mission,
            pending=lambda _mission: [], failure_ledger_dir=self.state_dir,
            digest=digest, clock=lambda: now[0],
        )
        result = coordinator.dispatch_once()
        self.assertEqual(result["status"], "idle")
        self.assertEqual(result["digests"]["status"], "unavailable")
        coordinator.dispatch_once()
        self.assertEqual(len(attempts), 2)


if __name__ == "__main__":
    unittest.main()
