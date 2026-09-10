"""P14f end to end: preview, then the print, then the calibration.

The blueprint's acceptance sentence for this slice is that one company's
results go from preview to reconciliation to calibration to a thesis
revision candidate on their own, and that a person decides once. This file is
that sentence, run against a Core on disk with two fake models standing in for
the broker -- no network, no real call, and a verifier on a different family so
the independence check is exercised rather than bypassed.
"""

from __future__ import annotations

import json
import unittest
from unittest import mock

from dalton_core import earnings_season as season
from dalton_core import mission_earnings_season_lane as lane
from dalton_core.earnings_preview import publish_preview
from dalton_core.earnings_season_cli import run_earnings_season
from dalton_core.mission_deliverable import MissionDeliverableConflict
from dalton_core.event_judgement import EventJudgementAuthority
from dalton_core.mission_deliverable import MissionDeliverableAuthority
from dalton_core.model_forecast_driver import ForecastModelAuthority
from dalton_core.research_event import ResearchEventAuthority, record_event
from tests.p14a_fixtures import ACN, AUTOMATION, EPAM, P14aHarness
from dalton_core.catalyst_calendar import calendar_event_payload
from tests.test_earnings_season import TODAY, calendar_entry
from dalton_core.company_model_inputs import build_model_inputs
from tests.test_model_forecast_driver import (
    cells_all,
    filed_quarter,
    ledger,
    model,
    spec,
)

from datetime import datetime, timezone


class FakeModel:
    """One canned answer per purpose, and a route this run can resolve."""

    def __init__(self, answers, *, family, prefix):
        self.answers = answers
        self.family = family
        self.prefix = prefix
        self.calls = []

    def call(self, *, purpose, request_id, prompt, mission):
        self.calls.append({"purpose": purpose, "request_id": request_id,
                           "prompt": prompt})
        body = self.answers[purpose]
        text = body(prompt) if callable(body) else body
        index = len(self.calls)
        return {
            "text": json.dumps(text, ensure_ascii=False),
            "work_order_ref": f"work:{self.prefix}:{index}",
            "invocation_ref": f"invocation:{self.prefix}:{index}",
            "result_envelope_ref": f"envelope:{self.prefix}:{index}",
            "route_decision_ref": f"route:{self.prefix}",
            "replayed": False,
            "cost_micros": 1000,
        }


def families(mapping):
    def resolve(route_decision_ref):
        return mapping.get(route_decision_ref)

    return resolve


PASS = {"verdict": "pass", "findings": [{"code": "ok", "detail": "nothing invented"}]}


class EndToEndTests(P14aHarness):
    grants = ("market_event", "observation", "stage_record", "deliverable",
              "forecast_line", "thesis_revision_candidate")

    def setUp(self) -> None:
        super().setUp()
        self.pass_screen(ACN)
        self.events = ResearchEventAuthority(self.store)
        self.judgements = EventJudgementAuthority(self.store)
        self.deliverables = MissionDeliverableAuthority(self.store)
        self.models = ForecastModelAuthority(self.store)
        self.models.publish(model())
        self.claim_ref = self.claim(
            subject=ACN, statement="ACN 报告本季度收入 1500000000 美元。",
            value="1500000000")
        self.resolver = families({"route:writer": "alpha", "route:verifier": "beta"})
        self.thesis_ref = "thesis-version:acn-1"
        self.thesis = {
            "ref": self.thesis_ref, "thesis_ref": "thesis:acn:demand-bottoming",
            "statement": "IT 服务需求在触底", "mechanism": "预算解冻",
            "confidence": "medium", "content_hash": "c" * 64,
            "falsifier_refs": [], "subject_ref": ACN,
        }
        self.table = build_model_inputs(filed_quarter(ledger()), spec())

    def regrant(self, *scopes):
        """Publish a mission version granting exactly these scopes.

        The harness's own ``grant`` only ever adds, and half of what is being
        tested here is what happens when a word is *not* granted.
        """

        params = dict(self.params)
        autonomy = dict(params["autonomy"])
        autonomy["may_write"] = list(scopes)
        autonomy["human_checkpoints"] = list(
            dict.fromkeys(list(autonomy["human_checkpoints"])
                          + ["thesis_revision_candidate", "forecast_overturn"]))
        params["autonomy"] = autonomy
        self._version += 1
        params.update({
            "version_id": f"coverage-mission-version:us-it-services:{self._version}",
            "prior_version_ref": self.mission["id"],
            "idempotency_key": f"coverage-mission:us-it-services:{self._version}",
        })
        self.mission = self.missions.create_mission(self.mission_ref, **params)
        return self.mission

    def calendar(self, *, expected, confirmed, window=None, anchor=None,
                 company_ref=ACN):
        """A calendar event on the ledger, built the way C1 builds one."""

        entry = calendar_entry(
            company_ref=company_ref, expected=expected, anchor=anchor or expected,
            confidence="confirmed" if confirmed else "estimated")
        return record_event(
            self.events, company_ref=company_ref, kind="calendar",
            occurred_at=f"{TODAY}T00:00:00+00:00",
            source_refs=["catalyst-calendar-version:1"],
            payload=calendar_event_payload(
                entry,
                window=window or ("calibration" if confirmed and
                                  expected <= TODAY else "preview"),
                version_ref="catalyst-calendar-version:1"),
            mission=self.mission, actor_ref=AUTOMATION,
        )

    def preview_answer(self, _prompt):
        return {
            "summary": "我们的预测落在指引区间上沿附近；日期未确认，按预期日期写。",
            "what_to_watch": [{
                "question": "book-to-bill 有没有回到 1 以上",
                "why": "预算解冻最早在这里看得见",
                "refs": [self.claim_ref],
            }],
            "confirms_thesis": [],
            "breaks_thesis": [{
                "observable": "咨询业务再降一个季度",
                "thesis_ref": self.thesis_ref,
                "refs": [self.thesis_ref],
            }],
            "citations": [self.claim_ref, self.thesis_ref],
        }

    def calibration_answer(self, _prompt):
        return {
            "summary": "收入好于我们的预测，机制第一次在数字上看得见。",
            "lines": [{"line": "revenue", "verdict": "better",
                       "because": "高于我们的假设", "refs": [self.claim_ref]}],
            "theses": [{
                "thesis_ref": self.thesis_ref,
                "decision": "THESIS_STRENGTHENED",
                "action": "revise_thesis",
                "because": "预算解冻这条机制第一次在数字上看得见",
                "proposed_statement": "IT 服务需求已经触底并开始回升",
                "refs": [self.claim_ref],
            }],
            "reflection": {
                "what_we_expected": "与我们的假设一致",
                "what_happened": "高于我们的假设",
                "why": "定价提升快于我们的预期",
                "citations": [self.claim_ref],
                "missed_debates": [],
                "followup_tracking": [],
                "followup_research": [],
                "market_view_vs_ours": {
                    "available": False, "our_direction": "long",
                    "summary": "这个 Core 没有 consensus", "refs": []},
                "convergence_pathway": "再有一两个季度的定价数据",
            },
        }

    def execute(self, *, writer_answers, now=None, **kwargs):
        """One child run, with the two neighbouring slices stubbed by name.

        ``company_theses`` and ``input_table_for`` read authorities this slice
        does not own and this fixture does not build -- a ThesisVersion needs
        an admission decision behind it, and a model input table needs a
        published specification and filed statement lines. Both are read
        through one named function, so standing them up here is a stub at the
        seam rather than a rewrite of the run.
        """

        writer = FakeModel(writer_answers, family="alpha", prefix="writer")
        verifier = FakeModel(
            {"earnings_preview_verifier": PASS,
             "earnings_calibration_verifier": PASS},
            family="beta", prefix="verifier")
        with mock.patch(
            "dalton_core.earnings_season_cli.company_theses",
            return_value=[self.thesis],
        ), mock.patch(
            "dalton_core.earnings_season_cli.input_table_for",
            return_value=self.table,
        ):
            summary = run_earnings_season(
                state_dir=self.state_dir, summary_dir=self.state_dir,
                writer_model=writer, verifier_model=verifier,
                family_resolver=self.resolver,
                now=now or datetime.fromisoformat(f"{TODAY}T12:00:00+00:00"),
                **kwargs,
            )
        return summary, writer, verifier

    def test_nothing_due_costs_nothing(self):
        summary, writer, _verifier = self.execute(writer_answers={})
        self.assertEqual(summary["status"], "idle")
        self.assertEqual(summary["season_status"], "nothing_due")
        self.assertEqual(writer.calls, [])

    def test_a_dry_run_says_what_is_due_and_stops(self):
        self.calendar(expected="2026-10-01", confirmed=False)
        summary, writer, _verifier = self.execute(writer_answers={}, dry_run=True)
        self.assertEqual(summary["season_status"], "dry_run")
        self.assertEqual(summary["windows"][0]["window"], "preview")
        self.assertEqual(summary["windows"][0]["date_confidence"], "estimated")
        self.assertEqual(writer.calls, [])

    def test_the_preview_is_written_once_for_the_occurrence(self):
        self.calendar(expected="2026-10-01", confirmed=False)
        summary, writer, verifier = self.execute(
            writer_answers={"earnings_preview": self.preview_answer})
        self.assertEqual(summary["status"], "succeeded", summary.get("failure_reason"))
        self.assertEqual(summary["previews"], 1)
        self.assertEqual(len(writer.calls), 1)
        self.assertEqual(len(verifier.calls), 1)
        record = self.deliverables.latest(f"mission-deliverable:earnings_preview:0001467373")
        self.assertIsNotNone(record)
        self.assertIn("日期未确认", record["sections"][0]["body"])
        # The company then confirms the date: a second calendar event, and no
        # second preview.
        self.calendar(expected="2026-10-01", confirmed=True)
        again, writer_two, _ = self.execute(
            writer_answers={"earnings_preview": self.preview_answer})
        self.assertEqual(again["season_status"], "nothing_due")
        self.assertEqual(writer_two.calls, [])

    def test_a_preview_whose_verifier_rejects_it_is_not_published(self):
        self.calendar(expected="2026-10-01", confirmed=False)
        writer = FakeModel({"earnings_preview": self.preview_answer},
                           family="alpha", prefix="writer")
        verifier = FakeModel({"earnings_preview_verifier": {
            "verdict": "reject",
            "findings": [{"code": "invented_number", "detail": "a figure with no Claim"}],
        }}, family="beta", prefix="verifier")
        summary = run_earnings_season(
            state_dir=self.state_dir, summary_dir=self.state_dir,
            writer_model=writer, verifier_model=verifier,
            family_resolver=self.resolver,
            now=datetime.fromisoformat(f"{TODAY}T12:00:00+00:00"),
        )
        self.assertEqual(summary["previews"], 0)
        self.assertEqual(summary["refused"], 1)
        self.assertIsNone(self.deliverables.latest(
            "mission-deliverable:earnings_preview:0001467373"))

    def test_a_verifier_on_the_producers_family_refuses_rather_than_passing(self):
        self.calendar(expected="2026-10-01", confirmed=False)
        summary, _writer, _verifier = self.execute(
            writer_answers={"earnings_preview": self.preview_answer})
        # Re-run with both routes resolving to one family.
        self.resolver = families({"route:writer": "alpha", "route:verifier": "alpha"})
        self.calendar(expected="2027-01-05", confirmed=False)
        again, _writer, _verifier = self.execute(
            writer_answers={"earnings_preview": self.preview_answer},
            now=datetime.fromisoformat("2026-12-20T12:00:00+00:00"))
        self.assertEqual(again["previews"], 0)
        self.assertEqual(again["refused"], 1)
        self.assertIn("not_independent", again["windows"][0]["reason"])

    def test_the_print_produces_a_calibration_and_one_thing_to_decide(self):
        # The acceptance sentence: preview, the company reports, calibration,
        # a candidate. Nobody approves anything along the way, and the one
        # thing waiting at the end is the candidate.
        self.calendar(expected="2026-10-01", confirmed=False)
        first, _writer, _verifier = self.execute(
            writer_answers={"earnings_preview": self.preview_answer})
        self.assertEqual(first["previews"], 1)

        self.calendar(expected="2026-09-08", confirmed=True)
        second, writer, verifier = self.execute(
            writer_answers={"earnings_calibration": self.calibration_answer})
        self.assertEqual(second["status"], "succeeded",
                         second.get("failure_reason") or second["windows"])
        self.assertEqual(second["calibrations"], 1)
        self.assertEqual(second["decisions"], {"THESIS_STRENGTHENED": 1})
        self.assertEqual(len(writer.calls), 1)
        self.assertEqual(len(verifier.calls), 1)

        window = second["windows"][0]
        # The model's history caught up with the filing...
        self.assertEqual(window["actualisation"], "fresh")
        # ...the calibration was published...
        self.assertIsNotNone(window["deliverable_ref"])
        # ...the judgement lane was told the quarter settled and that the
        # forward view is unreviewed...
        event = self.events.event(window["event_ref"])
        self.assertEqual(event["kind"], "calibration")
        self.assertIs(event["payload"]["forward_estimates_revised"], False)
        # ...and exactly one thing is waiting for a person.
        candidates = self.judgements.thesis_candidates(ACN)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["checkpoint_kind"], "thesis_revision_candidate")
        self.assertIsNotNone(candidates[0]["reflection_ref"])
        self.assertEqual(
            self.store.connection.execute(
                "SELECT COUNT(*) AS n FROM thesis_versions").fetchone()["n"], 0)

    def test_the_calibration_does_not_move_a_forward_quarter(self):
        self.calendar(expected="2026-09-08", confirmed=True)
        before = {cell["period"]["end"]: cell["value"]
                  for cell in cells_all(self.models.latest(ACN), "result:revenue")}
        self.execute(writer_answers={"earnings_calibration": self.calibration_answer})
        after = {cell["period"]["end"]: cell["value"]
                 for cell in cells_all(self.models.latest(ACN), "result:revenue")
                 if cell["kind"] == "estimate"}
        for end, value in after.items():
            if end == "2026-08-31":
                continue
            self.assertEqual(value, before[end])

    def test_a_second_run_over_the_same_occurrence_pays_nothing(self):
        self.calendar(expected="2026-09-08", confirmed=True)
        self.execute(writer_answers={"earnings_calibration": self.calibration_answer})
        again, writer, _verifier = self.execute(
            writer_answers={"earnings_calibration": self.calibration_answer})
        self.assertEqual(again["season_status"], "nothing_due")
        self.assertEqual(writer.calls, [])

    def test_a_company_due_today_that_has_not_filed_waits_instead_of_paying(self):
        # The window opens on the day the company is due, and the filing does
        # not land at midnight. A calibration written first would grade a print
        # nobody has read -- and would spend the occurrence doing it.
        self.calendar(expected="2026-09-08", confirmed=True)
        self.table = build_model_inputs(ledger(), spec())  # nothing filed yet
        waiting, writer, verifier = self.execute(
            writer_answers={"earnings_calibration": self.calibration_answer})
        self.assertEqual(waiting["season_status"], "waiting")
        self.assertEqual(waiting["windows"][0]["status"], "waiting")
        self.assertEqual(writer.calls, [])
        self.assertEqual(verifier.calls, [])

        # The filing lands. The same occurrence is still open, and now it is
        # written.
        self.table = build_model_inputs(filed_quarter(ledger()), spec())
        written, writer, _verifier = self.execute(
            writer_answers={"earnings_calibration": self.calibration_answer})
        self.assertEqual(written["calibrations"], 1)
        self.assertEqual(len(writer.calls), 1)

    def test_an_ungranted_market_event_leaves_the_occurrence_re_runnable(self):
        self.calendar(expected="2026-09-08", confirmed=True)
        self.regrant("deliverable", "forecast_line", "observation", "stage_record")
        blocked, writer, _verifier = self.execute(
            writer_answers={"earnings_calibration": self.calibration_answer})
        self.assertEqual(blocked["season_status"], "blocked")
        self.assertEqual(blocked["windows"][0]["status"], "ungranted")
        # Nothing was paid for, nothing was published, and nothing was burnt.
        self.assertEqual(writer.calls, [])
        self.assertIsNone(self.deliverables.latest(
            "mission-deliverable:earnings_calibration:0001467373"))
        self.regrant("deliverable", "forecast_line", "observation", "stage_record",
                     "market_event", "thesis_revision_candidate")
        written, writer, _verifier = self.execute(
            writer_answers={"earnings_calibration": self.calibration_answer})
        self.assertEqual(written["calibrations"], 1)
        self.assertEqual(len(self.judgements.thesis_candidates(ACN)), 1)

    def test_a_document_that_fails_to_publish_leaves_the_occurrence_open(self):
        # The half most easily lost is the half that reaches a person, so the
        # ledger writes go first; a document that then fails is a refusal of
        # this occurrence, and the next tick redoes the half that is missing.
        self.calendar(expected="2026-09-08", confirmed=True)
        broken = mock.patch(
            "dalton_core.earnings_season_cli.publish_calibration",
            side_effect=MissionDeliverableConflict("a figure cites a retired Claim"))
        with broken:
            refused, _writer, _verifier = self.execute(
                writer_answers={"earnings_calibration": self.calibration_answer})
        self.assertEqual(refused["season_status"], "refused")
        self.assertEqual(refused["windows"][0]["status"], "refused")
        self.assertIn("retired Claim", refused["windows"][0]["reason"])
        # The judgement and the candidate survived, and the occurrence is not
        # reported as done, because its document is missing.
        self.assertEqual(len(self.judgements.thesis_candidates(ACN)), 1)
        occurrence = lane.due_occurrences(
            self.store, self.missions, self.mission,
            now=datetime.fromisoformat(f"{TODAY}T12:00:00+00:00"))
        self.assertEqual([row["window"] for row in occurrence], ["calibration"])

        # The next tick publishes it, and the judgement it already has is not
        # written twice.
        written, _writer, _verifier = self.execute(
            writer_answers={"earnings_calibration": self.calibration_answer})
        self.assertEqual(written["calibrations"], 1)
        self.assertEqual(len(self.judgements.thesis_candidates(ACN)), 1)

    def test_one_refused_document_does_not_abandon_the_other_companies(self):
        self.pass_screen(EPAM)
        self.calendar(expected="2026-10-01", confirmed=False)
        self.calendar(company_ref=EPAM, expected="2026-10-01", confirmed=False)
        calls = {"n": 0}
        real = publish_preview

        def flaky(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise MissionDeliverableConflict("a figure cites a retired Claim")
            return real(*args, **kwargs)

        def answer(_prompt):
            # Cites only the thesis, so the same answer is valid for either
            # company: what is being tested is the run, not the drafting.
            body = self.preview_answer(_prompt)
            body["what_to_watch"][0]["refs"] = [self.thesis_ref]
            body["citations"] = [self.thesis_ref]
            return body

        with mock.patch("dalton_core.earnings_season_cli.publish_preview", flaky):
            summary, _writer, _verifier = self.execute(
                writer_answers={"earnings_preview": answer})
        self.assertEqual(summary["status"], "succeeded")
        self.assertEqual(summary["refused"], 1)
        self.assertEqual(summary["previews"], 1)

    def test_a_mission_that_grants_no_deliverable_pays_for_nothing(self):
        self.calendar(expected="2026-10-01", confirmed=False)
        params = dict(self.params)
        autonomy = dict(params["autonomy"])
        autonomy["may_write"] = ["observation"]
        params["autonomy"] = autonomy
        params.update({
            "version_id": "coverage-mission-version:us-it-services:99",
            "prior_version_ref": self.mission["id"],
            "idempotency_key": "coverage-mission:us-it-services:99",
        })
        self.missions.create_mission(self.mission_ref, **params)
        summary, writer, _verifier = self.execute(
            writer_answers={"earnings_preview": self.preview_answer})
        self.assertEqual(summary["season_status"], "ungranted")
        self.assertEqual(writer.calls, [])


if __name__ == "__main__":
    unittest.main()
