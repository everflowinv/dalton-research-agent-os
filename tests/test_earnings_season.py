"""P14f: the two windows, and the things they refuse to do.

The arithmetic is the forecast layer's own fixture -- revenue growing ten
percent a quarter, a fifth quarter arriving in a filing at 1.5bn against an
estimate of 1.4641bn -- so a failure here means this slice changed rather than
that a number drifted.

What most of the file is about is the refusals, because they are what makes
the two documents worth anything: a preview is written once per occurrence
whether the date was estimated or confirmed, a calibration of an unconfirmed
date is refused outright, a thesis revision is a candidate a person rules on
and never a commit, the forward quarters are not touched, and an answer that
cites a ref the prompt never showed is refused whole rather than repaired.
"""

from __future__ import annotations

import json
import unittest
from datetime import date

from dalton_core import earnings_calibration as calibration
from dalton_core import earnings_preview as preview
from dalton_core import earnings_season as season
from dalton_core.event_judgement import EventJudgementAuthority
from dalton_core.mission_deliverable import (
    DELIVERABLE_KINDS,
    MissionDeliverableAuthority,
)
from dalton_core.model_forecast_driver import (
    ForecastModelAuthority,
    actualize_model,
    replay_cell,
)
from dalton_core.research_event import (
    DEFAULT_TIER_BY_KIND,
    EVENT_KINDS,
    PAYLOAD_FIELDS,
    ResearchEventAuthority,
    record_event,
)
from dalton_core.store import DaltonStore, content_hash
from tests.p14a_fixtures import ACN, AUTOMATION, P14aHarness
from tests.test_model_forecast_driver import (
    cells_all,
    filed_quarter,
    ledger,
    model,
    spec,
)
from tests.test_company_model_inputs import ACN as MODEL_ACN
from dalton_core.company_model_inputs import build_model_inputs

TODAY = "2026-09-09"
REPORT_DATE = "2026-10-01"


def calendar_event(
    *, company_ref=ACN, window="preview", expected=REPORT_DATE,
    confidence="estimated", anchor=REPORT_DATE, entry="calendar-entry:acn:q4",
    payload=None, event_ref="research-event:preview-1",
):
    """A calendar event in C1's rich shape, unless one is supplied."""

    body = payload if payload is not None else {
        "event_key": "calendar-event:1",
        "window": window,
        "entry_ref": entry,
        "event_kind": "earnings",
        "subject": "FY26 Q4",
        "anchor_date": anchor,
        "expected_date": expected,
        "date_confidence": confidence,
        "date_unconfirmed": confidence != "confirmed",
        "date_caveat": "" if confidence == "confirmed" else "日期未确认",
        "disagreement": False,
        "days_until": (date.fromisoformat(expected) - date.fromisoformat(TODAY)).days,
        "as_of": TODAY,
    }
    return {
        "id": event_ref,
        "company_ref": company_ref,
        "kind": "calendar",
        "occurred_at": f"{TODAY}T00:00:00+00:00",
        "content_hash": content_hash({"event": event_ref}),
        "source_refs": ["catalyst-calendar-version:1"],
        "payload": body,
    }


class WindowTests(unittest.TestCase):
    """Which occurrence is open, and which one may not be."""

    def test_an_estimated_date_opens_the_preview(self):
        opened = season.window_of(calendar_event(), now=TODAY)
        self.assertEqual(opened, "preview")

    def test_an_estimated_date_never_opens_the_calibration(self):
        # C1's rule, re-checked here: a calibration is written *about* a
        # release, and an estimated date says a report was likely rather than
        # that one happened.
        event = calendar_event(window="calibration", expected="2026-09-08")
        self.assertIsNone(season.window_of(event, now=TODAY))

    def test_a_confirmed_date_opens_the_calibration(self):
        event = calendar_event(
            window="calibration", expected="2026-09-08", confidence="confirmed")
        self.assertEqual(season.window_of(event, now=TODAY), "calibration")

    def test_the_ledgers_narrower_payload_is_read_too(self):
        # The event ledger declares five fields for a calendar event and C1
        # writes eleven; this slice must work whichever wrote the row.
        event = calendar_event(payload={
            "event_kind": "earnings", "expected_date": REPORT_DATE,
            "confirmed": False, "calendar_version_ref": "catalyst-calendar-version:1",
            "source_ref": "connector-invocation:yfinance:1",
        })
        self.assertEqual(season.window_of(event, now=TODAY), "preview")
        occurrence = season.occurrence_of(event, now=TODAY)
        self.assertEqual(occurrence["date_confidence"], "estimated")
        self.assertEqual(occurrence["date_caveat"], "日期未确认")

    def test_a_date_change_is_not_a_window_this_lane_opens(self):
        event = calendar_event(window="date_change")
        self.assertIsNone(season.window_of(event, now=TODAY))

    def test_a_report_a_year_out_opens_nothing(self):
        event = calendar_event(payload={
            "event_kind": "earnings", "expected_date": "2027-10-01",
            "confirmed": True, "calendar_version_ref": None, "source_ref": None,
        })
        self.assertIsNone(season.window_of(event, now=TODAY))

    def test_the_caveat_travels_on_the_occurrence(self):
        occurrence = season.occurrence_of(calendar_event(), now=TODAY)
        self.assertTrue(occurrence["date_unconfirmed"])
        self.assertEqual(occurrence["date_caveat"], "日期未确认")

    def test_the_occurrence_is_the_same_when_the_date_moves(self):
        # A date that moves is the same report. Keying the work on the date
        # would buy a second preview every time Yahoo changed its mind.
        first = season.occurrence_of(calendar_event(), now=TODAY)
        moved = season.occurrence_of(
            calendar_event(expected="2026-10-03"), now=TODAY)
        self.assertEqual(first["occurrence_ref"], moved["occurrence_ref"])

    def test_confirming_the_date_does_not_change_the_occurrence(self):
        estimated = season.occurrence_of(calendar_event(), now=TODAY)
        confirmed = season.occurrence_of(
            calendar_event(confidence="confirmed", event_ref="research-event:preview-2"),
            now=TODAY,
        )
        self.assertEqual(estimated["occurrence_ref"], confirmed["occurrence_ref"])
        self.assertEqual(
            season.idempotency_key_for("preview", estimated),
            season.idempotency_key_for("preview", confirmed),
        )


class GuidanceTests(unittest.TestCase):
    """The named function the dossier and the weekly brief can call too."""

    profile = {
        "classification": {"style": "beat_and_raise"},
        "events": [
            {
                "period": "2026-08-31", "measure": "revenue",
                "guide": {"low": "1400", "high": "1450", "unit": "usd",
                          "guide_basis": "range", "refs": ["claim-version:g1"]},
                "actual": {"value": "1500", "unit": "usd", "refs": ["claim-version:a1"]},
                "deviation": {"verdict": "beat", "distance": "50",
                              "reason": "above the top of the guided range"},
            },
            {
                "period": "2026-05-31", "measure": "revenue",
                "guide": {"low": "1300", "high": "1350", "unit": "usd",
                          "guide_basis": "range", "refs": ["claim-version:g0"]},
                "actual": None,
                "deviation": {"verdict": "unknown", "distance": None,
                              "reason": "no actual for this period and measure"},
            },
        ],
    }

    def test_one_period_is_read_back_with_its_verdict_and_refs(self):
        answer = season.guidance_vs_actual(self.profile, "2026-08-31")
        self.assertTrue(answer["available"])
        self.assertEqual([row["verdict"] for row in answer["rows"]], ["beat"])
        self.assertEqual(answer["settled"], 1)
        self.assertEqual(answer["refs"], ["claim-version:a1", "claim-version:g1"])
        self.assertEqual(answer["classification"], "beat_and_raise")

    def test_no_profile_is_available_false_and_a_reason(self):
        answer = season.guidance_vs_actual(None, "2026-08-31")
        self.assertFalse(answer["available"])
        self.assertIn("no guidance profile", answer["reason"])
        self.assertEqual(answer["rows"], [])

    def test_a_period_the_profile_has_nothing_for_says_so(self):
        answer = season.guidance_vs_actual(self.profile, "2027-02-28")
        self.assertFalse(answer["available"])
        self.assertIn("no event for this period", answer["reason"])


class ConsensusTests(unittest.TestCase):
    """``available: false`` is an answer, and the only honest one today."""

    def test_no_reader_is_available_false_with_the_reason(self):
        block = season.consensus_block(ACN, "2026-08-31", reader=None)
        self.assertFalse(block["available"])
        self.assertIn("P11b", block["reason"])
        self.assertEqual(block["refs"], [])

    def test_a_reader_that_answers_is_read_with_its_refs(self):
        def reader(_company_ref):
            return {"estimates": [{
                "metric": "revenue", "value": "1480", "unit": "usd",
                "estimates": 7, "period_end": "2026-08-31",
                "refs": ["consensus-estimate-version:1"],
            }]}

        block = season.consensus_block(ACN, "2026-08-31", reader=reader)
        self.assertTrue(block["available"])
        self.assertEqual(block["rows"][0]["value"], "1480")
        self.assertEqual(block["refs"], ["consensus-estimate-version:1"])

    def test_a_consensus_figure_with_no_ref_is_not_a_consensus_figure(self):
        def reader(_company_ref):
            return {"estimates": [{"metric": "revenue", "value": "1480", "refs": []}]}

        block = season.consensus_block(ACN, "2026-08-31", reader=reader)
        self.assertFalse(block["available"])
        self.assertIn("with a ref", block["reason"])

    def test_a_reader_that_raises_does_not_take_the_window_with_it(self):
        def reader(_company_ref):
            raise RuntimeError("the consensus store is not built yet")

        block = season.consensus_block(ACN, "2026-08-31", reader=reader)
        self.assertFalse(block["available"])
        self.assertIn("RuntimeError", block["reason"])


class ForecastReadingTests(unittest.TestCase):
    """Which quarter a report is about, and what we said about it."""

    def setUp(self) -> None:
        self.model = model()

    def test_the_reported_quarter_is_the_last_one_that_closed(self):
        period = season.reported_period(self.model, REPORT_DATE)
        self.assertEqual(period["end"], "2026-08-31")

    def test_every_row_carries_the_cell_and_what_it_was_computed_from(self):
        rows = season.forecast_rows(self.model, "2026-08-31")
        revenue = next(row for row in rows if row["line_ref"] == "result:revenue")
        self.assertEqual(revenue["kind"], "estimate")
        self.assertIn("result:revenue@2026-08-31:estimate", revenue["refs"])
        self.assertTrue(any(ref.startswith("assumption:") for ref in revenue["refs"]))

    def test_a_company_with_no_model_has_no_period_and_no_rows(self):
        self.assertIsNone(season.reported_period(None, REPORT_DATE))
        self.assertEqual(season.forecast_rows(None, "2026-08-31"), [])


class WatchListTests(unittest.TestCase):
    def test_open_debates_win_when_there_are_any(self):
        answer = season.watch_list(
            debates=[{"question": "增长是不是结构性的", "refs": ["debate-map-version:1"]}],
            theses=[{"ref": "thesis-version:1", "mechanism": "需求回暖"}],
        )
        self.assertEqual(answer["source"], "open_debates")
        self.assertEqual(answer["rows"][0]["source"], "open_debate")

    def test_the_fallback_is_the_thesis_and_the_drivers_and_says_so(self):
        answer = season.watch_list(
            theses=[{"ref": "thesis-version:1", "mechanism": "需求回暖",
                     "falsifier_refs": ["falsifier:1"]}],
            drivers=[{"ref": "driver:us-gaap:Revenues", "label": "Revenue"}],
        )
        self.assertEqual(answer["source"], "thesis_falsifiers_and_drivers")
        self.assertEqual(
            [row["source"] for row in answer["rows"]],
            ["thesis_falsifier", "model_driver"])
        self.assertIn("falsifier:1", answer["rows"][0]["refs"])


# ---------------------------------------------------------------------------
# the preview
# ---------------------------------------------------------------------------


class PreviewHarness(P14aHarness):
    grants = ("market_event", "observation", "stage_record", "deliverable",
              "forecast_line", "thesis_revision_candidate")

    def setUp(self) -> None:
        super().setUp()
        self.pass_screen(ACN)
        self.deliverables = MissionDeliverableAuthority(self.store)
        self.events = ResearchEventAuthority(self.store)
        self.judgements = EventJudgementAuthority(self.store)
        self.claim_ref = self.claim(
            subject=ACN, statement="ACN 报告本季度收入 1500000000 美元。", value="1500000000")
        self.models = ForecastModelAuthority(self.store)
        self.model = self.models.publish(model())
        self.occurrence = season.occurrence_of(calendar_event(), now=TODAY)
        self.context = preview.build_preview_context(
            occurrence=self.occurrence, mission=self.mission,
            model_version=self.model,
            guidance_profile=GuidanceTests.profile,
            theses=[{"ref": "thesis-version:1", "thesis_ref": "thesis:1",
                     "statement": "IT 服务需求在触底", "mechanism": "预算解冻",
                     "confidence": "medium", "content_hash": "c" * 64}],
            claims=[{"ref": self.claim_ref,
                     "text": "ACN 报告本季度收入 1500000000 美元。", "period": "2026Q4"}],
        )

    def answer(self, **overrides):
        body = {
            "summary": "我们对这一季的看法与指引区间上沿一致，日期未确认。",
            "what_to_watch": [{
                "question": "book-to-bill 有没有回到 1 以上",
                "why": "这是预算解冻最早能被看见的地方",
                "refs": ["thesis-version:1"],
            }],
            "confirms_thesis": [{
                "observable": "新签订单同比转正",
                "thesis_ref": "thesis-version:1",
                "refs": ["thesis-version:1"],
            }],
            "breaks_thesis": [{
                "observable": "咨询业务再降一个季度",
                "thesis_ref": "thesis-version:1",
                "refs": ["thesis-version:1"],
            }],
            "citations": ["thesis-version:1", "claim-version:" + "0" * 63 + "1"],
        }
        body["citations"] = ["thesis-version:1", self.claim_ref]
        body.update(overrides)
        return body


class PreviewContextTests(PreviewHarness):
    def test_the_context_carries_our_numbers_the_street_and_the_guidance(self):
        self.assertEqual(self.context["period_end"], "2026-08-31")
        self.assertTrue(self.context["forecast"])
        self.assertFalse(self.context["consensus"]["available"])
        self.assertTrue(self.context["guidance"]["available"])

    def test_an_unavailable_block_becomes_a_gap_in_the_document(self):
        self.assertTrue(any("consensus" in gap for gap in self.context["gaps"]))

    def test_the_prompt_says_not_to_print_our_own_forecast(self):
        prompt = preview.build_preview_prompt(self.context)
        self.assertIn("我们自己的预测数字不要写进正文", prompt)
        self.assertIn("日期未确认", prompt)

    def test_the_summary_line_carries_every_figure_with_its_ref(self):
        line = preview.document_summary(self.context)
        self.assertIn("我们 ", line)
        self.assertIn(self.model["id"], line)
        self.assertIn("街上 available:false", line)
        self.assertIn("日期未确认", line)


class PreviewOutputTests(PreviewHarness):
    def test_a_well_formed_answer_is_accepted(self):
        checked = preview.validate_preview_output(self.answer(), self.context)
        self.assertEqual(len(checked["what_to_watch"]), 1)

    def test_a_citation_that_was_never_shown_refuses_the_whole_answer(self):
        with self.assertRaises(season.EarningsSeasonValidationError) as caught:
            preview.validate_preview_output(
                self.answer(citations=["claim-version:invented"]), self.context)
        self.assertIn("were not shown", str(caught.exception))

    def test_a_sixth_key_refuses_the_whole_answer(self):
        with self.assertRaises(season.EarningsSeasonValidationError):
            preview.validate_preview_output(
                {**self.answer(), "conviction": "high"}, self.context)

    def test_a_thesis_it_was_not_shown_is_refused(self):
        with self.assertRaises(season.EarningsSeasonValidationError) as caught:
            preview.validate_preview_output(self.answer(breaks_thesis=[{
                "observable": "x", "thesis_ref": "thesis-version:9", "refs": [],
            }]), self.context)
        self.assertIn("not shown", str(caught.exception))

    def test_a_figure_with_no_live_claim_behind_it_is_refused(self):
        with self.assertRaises(season.EarningsSeasonValidationError) as caught:
            preview.validate_preview_output(
                self.answer(summary="我们看 1750000000 美元的收入。日期未确认。"),
                self.context)
        self.assertIn("no live Claim", str(caught.exception))

    def test_a_figure_a_shown_claim_accounts_for_is_allowed(self):
        checked = preview.validate_preview_output(
            self.answer(summary="街上和我们都盯着 1500000000 这个数。日期未确认。"),
            self.context)
        self.assertIn("1500000000", checked["summary"])

    def test_an_unconfirmed_date_with_no_caveat_is_refused(self):
        with self.assertRaises(season.EarningsSeasonValidationError) as caught:
            preview.validate_preview_output(
                self.answer(summary="这一季我们与指引一致。"), self.context)
        self.assertIn("not confirmed", str(caught.exception))

    def test_a_preview_that_cannot_say_what_would_break_the_thesis_is_refused(self):
        with self.assertRaises(season.EarningsSeasonValidationError) as caught:
            preview.validate_preview_output(self.answer(breaks_thesis=[]), self.context)
        self.assertIn("break the thesis", str(caught.exception))

    def test_a_preview_with_nothing_to_watch_is_refused(self):
        with self.assertRaises(season.EarningsSeasonValidationError) as caught:
            preview.validate_preview_output(self.answer(what_to_watch=[]), self.context)
        self.assertIn("nothing to watch", str(caught.exception))


class PreviewPublishTests(PreviewHarness):
    def published(self, context=None):
        context = context or self.context
        return preview.publish_preview(
            self.deliverables, context=context,
            draft={"status": "drafted", "model": {"invocation_ref": "invocation:1"},
                   **preview.validate_preview_output(self.answer(), context)},
            mission=self.mission, playbook=self.playbook, actor_ref=AUTOMATION,
        )

    def test_the_preview_is_a_deliverable_on_the_companys_own_chain(self):
        record = self.published()
        self.assertEqual(record["kind"], "earnings_preview")
        self.assertEqual(record["subject_ref"], ACN)
        self.assertEqual(record["status"], "fresh")

    def test_the_same_occurrence_is_written_once(self):
        first = self.published()
        second = self.published()
        self.assertEqual(second["status"], "duplicate")
        self.assertEqual(first["id"], second["id"])

    def test_a_second_event_once_the_date_is_confirmed_writes_nothing_new(self):
        # C1 emits a second preview event when the company confirms a date it
        # had estimated. That is news for the judgement layer's schedule and
        # not a reason to pay for a second preview.
        self.published()
        confirmed = season.occurrence_of(
            calendar_event(confidence="confirmed",
                           event_ref="research-event:preview-2"),
            now=TODAY,
        )
        self.assertIsNotNone(
            season.already_written(self.store.connection, "preview", confirmed))

    def test_an_unwritten_occurrence_is_not_reported_as_written(self):
        other = season.occurrence_of(
            calendar_event(anchor="2027-01-05", expected="2027-01-05",
                           entry="calendar-entry:acn:q1"),
            now="2026-12-20",
        )
        self.assertIsNone(
            season.already_written(self.store.connection, "preview", other))


# ---------------------------------------------------------------------------
# the calibration
# ---------------------------------------------------------------------------


class CalibrationHarness(P14aHarness):
    grants = ("market_event", "observation", "stage_record", "deliverable",
              "forecast_line", "thesis_revision_candidate")

    def setUp(self) -> None:
        super().setUp()
        self.assertEqual(ACN, MODEL_ACN)
        self.pass_screen(ACN)
        self.deliverables = MissionDeliverableAuthority(self.store)
        self.events = ResearchEventAuthority(self.store)
        self.judgements = EventJudgementAuthority(self.store)
        self.models = ForecastModelAuthority(self.store)
        self.first = self.models.publish(model())
        self.table = build_model_inputs(filed_quarter(ledger()), spec())
        self.claim_ref = self.claim(
            subject=ACN, statement="ACN 报告本季度收入 1500000000 美元。",
            value="1500000000")
        self.guide_ref = self.claim(
            subject=ACN, statement="管理层指引本季度收入 1400000000 到 1450000000 美元。")
        self.thesis = {"ref": "thesis-version:1", "thesis_ref": "thesis:1",
                       "statement": "IT 服务需求在触底", "mechanism": "预算解冻",
                       "confidence": "medium", "content_hash": "c" * 64}
        self.occurrence = season.occurrence_of(
            calendar_event(window="calibration", expected="2026-09-08",
                           anchor="2026-09-08", confidence="confirmed",
                           event_ref="research-event:calibration-1"),
            now=TODAY,
        )

    def profile(self):
        """P12f's table for this quarter, with refs a Ledger actually holds."""

        return {
            "classification": {"style": "beat_and_raise"},
            "events": [{
                "period": "2026-08-31", "measure": "revenue",
                "guide": {"low": "1400", "high": "1450", "unit": "usd",
                          "guide_basis": "range", "refs": [self.guide_ref]},
                "actual": {"value": "1500", "unit": "usd", "refs": [self.claim_ref]},
                "deviation": {"verdict": "beat", "distance": "50",
                              "reason": "above the top of the guided range"},
            }],
        }

    def actualized(self):
        return calibration.actualize_for_report(
            self.models, company_ref=ACN, input_table=self.table,
            actor_ref=AUTOMATION)

    def rows(self, band="notable", deviation="-2.4000"):
        return [{
            "id": "forecast-reconciliation:1",
            "metric_ref": "metric:revenue-usd",
            "period_end": "2026-08-31",
            "forecast_value": "1464100000.00000000",
            "actual_value": "1500000000.00000000",
            "unit": "one",
            "deviation_percent": deviation,
            "direction": "above_forecast",
            "band": band,
            "human_checkpoint": "forecast_overturn" if band == "overturn_candidate" else None,
            "checkpoint_status": "pending" if band == "overturn_candidate" else "not_required",
            "forecast_line_version_ref": "model-forecast-line-version:1",
            "claim_version_ref": self.claim_ref,
        }]

    def context(self, *, rows=None, model_version=None, actualisation=None):
        return calibration.build_calibration_context(
            occurrence=self.occurrence, mission=self.mission,
            model_version=model_version or self.models.latest(ACN),
            actualisation=actualisation or {"status": "published",
                                            "version_ref": self.first["id"]},
            reconciliations=self.rows() if rows is None else rows,
            guidance_profile=self.profile(),
            theses=[self.thesis],
            claims=[{"ref": self.claim_ref,
                     "text": "ACN 报告本季度收入 1500000000 美元。", "period": "2026Q4"}],
            source_keys=["alphaengine", "sales-notes"],
        )

    def answer(self, **overrides):
        body = {
            "summary": "收入好于我们的预测，也高于指引上沿；驱动是价格而不是量。",
            "lines": [{
                "line": "revenue", "verdict": "better",
                "because": "高于指引区间上沿，也高于我们的假设",
                "refs": [self.guide_ref, self.claim_ref],
            }],
            "theses": [{
                "thesis_ref": "thesis-version:1",
                "decision": "THESIS_STRENGTHENED",
                "action": "revise_thesis",
                "because": "预算解冻这条机制第一次在数字上看得见",
                "proposed_statement": "IT 服务需求已经触底并开始回升",
                "refs": [self.claim_ref],
            }],
            "reflection": {
                "what_we_expected": "我们预期这一季与指引中值一致",
                "what_happened": "实际高于指引上沿",
                "why": "价格提升快于我们的假设",
                "citations": [self.claim_ref, self.guide_ref],
                "missed_debates": [{"question": "定价能不能持续", "refs": [self.claim_ref]}],
                "followup_tracking": [{
                    "source_key": "alphaengine", "interval_seconds": 43200,
                    "because": "覆盖这家公司的研报密度值得提高",
                }],
                "followup_research": [{"question": "定价从哪来", "wants": "客户合同条款"}],
                "market_view_vs_ours": {
                    "available": False, "our_direction": "long",
                    "summary": "这个 Core 没有 consensus，无法说街上怎么看", "refs": [],
                },
                "convergence_pathway": "再有一两个季度的定价数据，街上会跟上",
            },
        }
        body.update(overrides)
        return body


class CalibrationDeterministicTests(CalibrationHarness):
    """The three things that happen before anything is paid for."""

    def test_the_filed_quarter_lands_beside_the_estimate(self):
        outcome = self.actualized()
        self.assertEqual(outcome["status"], "fresh")
        self.assertEqual(outcome["change_reason"], "filing_actual")
        self.assertEqual(outcome["realised_ends"], ["2026-08-31"])
        cells = [cell for cell in cells_all(
            self.models.latest(ACN), "result:revenue")
            if cell["period"]["end"] == "2026-08-31"]
        estimate = next(item for item in cells if item["kind"] == "estimate")
        actual = next(item for item in cells if item["kind"] == "actual")
        self.assertEqual(estimate["value"], "1464100000.00000000")
        self.assertEqual(actual["value"], "1500000000.00000000")
        self.assertEqual(estimate["superseded_by"], actual["ref"])

    def test_the_quarters_still_ahead_are_not_touched(self):
        before = {cell["period"]["end"]: cell["value"]
                  for cell in cells_all(self.first, "result:revenue")}
        self.actualized()
        after = {cell["period"]["end"]: cell["value"]
                 for cell in cells_all(self.models.latest(ACN), "result:revenue")
                 if cell["kind"] == "estimate"}
        for end, value in after.items():
            if end == "2026-08-31":
                continue
            self.assertEqual(value, before[end])

    def test_what_we_thought_and_what_happened_can_both_be_replayed(self):
        self.actualized()
        history = replay_cell(
            self.models.versions(ACN), "result:revenue", "2026-08-31")
        self.assertEqual([item["kind"] for item in history],
                         ["estimate", "estimate", "actual"])

    def test_a_company_with_no_input_table_says_so_rather_than_raising(self):
        outcome = calibration.actualize_for_report(
            self.models, company_ref=ACN, input_table=None, actor_ref=AUTOMATION)
        self.assertEqual(outcome["status"], "unavailable")
        self.assertIn("input table", outcome["reason"])

    def test_a_quarter_nobody_has_filed_yet_is_idle(self):
        outcome = calibration.actualize_for_report(
            self.models, company_ref=ACN,
            input_table=build_model_inputs(ledger(), spec()), actor_ref=AUTOMATION)
        self.assertEqual(outcome["status"], "idle")

    def test_the_three_tiers_are_counted_and_the_overturn_named(self):
        counted = calibration.tier_counts(
            self.rows() + self.rows(band="overturn_candidate", deviation="4.2000"))
        self.assertEqual(counted["counts"],
                         {"notable": 1, "overturn_candidate": 1})
        self.assertTrue(counted["overturn_fired"])
        self.assertEqual(len(counted["overturn_candidates"]), 1)

    def test_within_tolerance_does_not_fire_the_human_checkpoint(self):
        counted = calibration.tier_counts(self.rows(band="within_tolerance"))
        self.assertFalse(counted["overturn_fired"])

    def test_the_actual_cells_are_handed_to_p12f_in_the_shape_it_reads(self):
        self.actualized()
        rows = calibration.actual_rows_for_guidance(
            self.models.latest(ACN), "2026-08-31")
        self.assertTrue(rows)
        for row in rows:
            self.assertEqual(set(row),
                             {"ref", "label", "text", "period", "period_end",
                              "value", "unit"})
            self.assertTrue(row["ref"])


class CalibrationContextTests(CalibrationHarness):
    def test_an_unconfirmed_date_is_refused_outright(self):
        occurrence = dict(self.occurrence, date_confidence="estimated")
        with self.assertRaises(calibration.CalibrationRefused) as caught:
            calibration.build_calibration_context(
                occurrence=occurrence, mission=self.mission,
                model_version=self.first, reconciliations=[])
        self.assertIn("not confirmed", str(caught.exception))

    def test_a_preview_occurrence_may_not_be_calibrated(self):
        occurrence = dict(self.occurrence, window="preview")
        with self.assertRaises(calibration.CalibrationRefused):
            calibration.build_calibration_context(
                occurrence=occurrence, mission=self.mission,
                model_version=self.first, reconciliations=[])

    def test_no_reconciliation_row_is_a_gap_and_not_a_silence(self):
        context = self.context(rows=[])
        self.assertTrue(any("对账行" in gap for gap in context["gaps"]))

    def test_the_prompt_carries_the_tiers_and_the_five_words(self):
        prompt = calibration.build_calibration_prompt(self.context())
        self.assertIn("THESIS_WEAKENED", prompt)
        self.assertIn("三档由 forecast_reconciliation 给出", prompt)
        self.assertIn("notable", prompt)
        self.assertIn("未来期的预测这一步一律不动", prompt)


class CalibrationOutputTests(CalibrationHarness):
    def test_a_well_formed_answer_is_accepted(self):
        checked = calibration.validate_calibration_output(
            self.answer(), self.context())
        self.assertEqual(checked["theses"][0]["decision"], "THESIS_STRENGTHENED")
        self.assertEqual(checked["reflection"]["thesis_refs"], ["thesis-version:1"])

    def test_no_change_may_not_be_a_reason_to_revise_the_thesis(self):
        answer = self.answer()
        answer["theses"][0]["decision"] = "NO_CHANGE"
        with self.assertRaises(season.EarningsSeasonValidationError) as caught:
            calibration.validate_calibration_output(answer, self.context())
        self.assertIn("not compatible", str(caught.exception))

    def test_a_sixth_decision_word_is_refused(self):
        answer = self.answer()
        answer["theses"][0]["decision"] = "THESIS_CONFIRMED"
        with self.assertRaises(season.EarningsSeasonValidationError):
            calibration.validate_calibration_output(answer, self.context())

    def test_a_verdict_outside_the_three_words_is_refused(self):
        answer = self.answer()
        answer["lines"][0]["verdict"] = "mixed"
        with self.assertRaises(season.EarningsSeasonValidationError):
            calibration.validate_calibration_output(answer, self.context())

    def test_a_market_view_that_is_unavailable_may_not_cite_anything(self):
        answer = self.answer()
        answer["reflection"]["market_view_vs_ours"]["refs"] = [self.claim_ref]
        with self.assertRaises(season.EarningsSeasonValidationError) as caught:
            calibration.validate_calibration_output(answer, self.context())
        self.assertIn("unavailable and then cites", str(caught.exception))

    def test_a_tracking_follow_up_naming_an_unknown_source_is_refused(self):
        answer = self.answer()
        answer["reflection"]["followup_tracking"][0]["source_key"] = "bloomberg"
        with self.assertRaises(season.EarningsSeasonValidationError) as caught:
            calibration.validate_calibration_output(answer, self.context())
        self.assertIn("no baseline cadence", str(caught.exception))

    def test_a_calibration_that_says_nothing_about_a_thesis_is_refused(self):
        with self.assertRaises(season.EarningsSeasonValidationError) as caught:
            calibration.validate_calibration_output(
                self.answer(theses=[]), self.context())
        self.assertIn("has not done the work", str(caught.exception))

    def test_a_revision_that_proposes_no_statement_must_be_a_broken_thesis(self):
        answer = self.answer()
        answer["theses"][0]["proposed_statement"] = None
        with self.assertRaises(season.EarningsSeasonValidationError) as caught:
            calibration.validate_calibration_output(answer, self.context())
        self.assertIn("proposes nothing", str(caught.exception))
        answer["theses"][0]["decision"] = "THESIS_BROKEN"
        checked = calibration.validate_calibration_output(answer, self.context())
        self.assertIsNone(checked["theses"][0]["proposed_statement"])

    def test_the_body_says_the_forward_view_was_not_touched(self):
        context = self.context()
        checked = calibration.validate_calibration_output(self.answer(), context)
        body = calibration.calibration_body(checked, context)
        self.assertIn("未来期的预测在这一步没有被修改", body)


class CalibrationEffectTests(CalibrationHarness):
    """Proposals, and never a commit."""

    def setUp(self) -> None:
        super().setUp()
        self.ctx = self.context()
        self.output = {
            **calibration.validate_calibration_output(self.answer(), self.ctx),
            "model": {"work_order_ref": "work:earnings-calibration:1",
                      "invocation_ref": "invocation:1", "cost_micros": 1000},
        }
        self.verification = {
            "status": "verified", "verdict": "pass",
            "findings": [{"code": "ok", "detail": "nothing invented"}],
            "independence": {"producer_family": "a", "verifier_family": "b",
                             "predicate": "model_family_ne"},
            "model": {"work_order_ref": "work:earnings-calibration:v1",
                      "cost_micros": 500},
        }

    def emit(self, output=None):
        return calibration.emit_calibration_event(
            context=self.ctx, output=output or self.output,
            record_event=lambda **kwargs: record_event(self.events, **kwargs),
            mission=self.mission, actor_ref=AUTOMATION,
        )

    def apply(self, output=None):
        event = self.emit(output)
        return event, calibration.apply_calibration_effects(
            self.judgements, event=event, context=self.ctx,
            output=output or self.output, verification=self.verification,
            mission=self.mission, actor_ref=AUTOMATION,
        )

    def test_the_event_tells_the_judgement_lane_the_forward_view_is_unreviewed(self):
        event = self.emit()
        self.assertEqual(event["kind"], "calibration")
        self.assertIs(event["payload"]["forward_estimates_revised"], False)
        self.assertEqual(event["payload"]["decision"], "THESIS_STRENGTHENED")
        self.assertEqual(event["payload"]["period_end"], "2026-08-31")

    def test_a_revision_becomes_a_candidate_a_person_rules_on(self):
        _event, effects = self.apply()
        self.assertEqual(effects["status"], "applied")
        candidates = self.judgements.thesis_candidates(ACN)
        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["checkpoint_kind"], "thesis_revision_candidate")
        self.assertEqual(candidates[0]["decision"], "THESIS_STRENGTHENED")
        self.assertEqual(candidates[0]["thesis_version_ref"], "thesis-version:1")

    def test_the_reflection_is_written_and_attached_to_the_candidate(self):
        _event, effects = self.apply()
        self.assertIsNotNone(effects["reflection_ref"])
        candidates = self.judgements.thesis_candidates(ACN)
        self.assertEqual(candidates[0]["reflection_ref"], effects["reflection_ref"])
        reflection = self.judgements.reflections(ACN)[0]
        self.assertEqual(reflection["trigger_kind"], "revision")
        self.assertFalse(reflection["market_view_vs_ours"]["available"])

    def test_no_thesis_version_is_written_by_any_of_this(self):
        # The whole point of ADR-0007: automation proposes, a person decides.
        self.apply()
        rows = self.store.connection.execute(
            "SELECT COUNT(*) AS n FROM thesis_versions").fetchone()
        self.assertEqual(rows["n"], 0)

    def test_the_forward_periods_are_the_same_after_the_effects(self):
        before = {cell["period"]["end"]: cell["value"]
                  for cell in cells_all(self.models.latest(ACN), "result:revenue")}
        self.apply()
        after = {cell["period"]["end"]: cell["value"]
                 for cell in cells_all(self.models.latest(ACN), "result:revenue")}
        self.assertEqual(before, after)

    def test_the_three_percent_tier_raises_a_forecast_overturn_proposal(self):
        self.ctx = self.context(
            rows=self.rows(band="overturn_candidate", deviation="4.2000"))
        self.output = {
            **calibration.validate_calibration_output(self.answer(), self.ctx),
            "model": {"work_order_ref": "work:earnings-calibration:2",
                      "cost_micros": 0},
        }
        _event, effects = self.apply()
        self.assertEqual(len(effects["forecast_proposals"]), 1)
        proposal = self.judgements.forecast_proposals(ACN)[0]
        self.assertEqual(proposal["checkpoint_kind"], "forecast_overturn")
        self.assertIn("overturn", proposal["because"])
        self.assertIn("human checkpoint", proposal["reason"])

    def test_a_within_tolerance_quarter_proposes_no_forecast_change(self):
        self.ctx = self.context(rows=self.rows(band="within_tolerance", deviation="0.4"))
        self.output = {
            **calibration.validate_calibration_output(self.answer(), self.ctx),
            "model": {"work_order_ref": "work:earnings-calibration:3", "cost_micros": 0},
        }
        _event, effects = self.apply()
        self.assertEqual(effects["forecast_proposals"], [])

    def test_a_no_change_calibration_still_writes_the_decision_down(self):
        answer = self.answer()
        answer["theses"][0].update({
            "decision": "NO_CHANGE", "action": "note", "proposed_statement": None,
        })
        output = {
            **calibration.validate_calibration_output(answer, self.ctx),
            "model": {"work_order_ref": "work:earnings-calibration:4", "cost_micros": 0},
        }
        _event, effects = self.apply(output)
        self.assertEqual(effects["decision"], "NO_CHANGE")
        self.assertEqual(effects["candidates"], [])
        self.assertIsNone(effects["reflection_ref"])

    def test_the_same_event_is_never_judged_twice(self):
        self.apply()
        _event, second = self.apply()
        self.assertEqual(second["status"], "duplicate")
        self.assertEqual(len(self.judgements.thesis_candidates(ACN)), 1)

    def test_the_calibration_is_published_once_per_occurrence(self):
        first = calibration.publish_calibration(
            self.deliverables, context=self.ctx, output=self.output,
            mission=self.mission, playbook=self.playbook, actor_ref=AUTOMATION)
        second = calibration.publish_calibration(
            self.deliverables, context=self.ctx, output=self.output,
            mission=self.mission, playbook=self.playbook, actor_ref=AUTOMATION)
        self.assertEqual(first["kind"], "earnings_calibration")
        self.assertEqual(second["status"], "duplicate")


class VocabularyTests(unittest.TestCase):
    """The additive words, and the ones that were already there."""

    def test_the_two_deliverable_kinds_exist_and_the_old_ones_are_untouched(self):
        self.assertIn("earnings_preview", DELIVERABLE_KINDS)
        self.assertIn("earnings_calibration", DELIVERABLE_KINDS)
        self.assertEqual(DELIVERABLE_KINDS[:8], (
            "industry_framework", "initial_screen", "industry_model",
            "company_model", "forecast_lines", "investment_memo", "weekly_brief",
            "event_note"))

    def test_the_calibration_event_kind_is_additive(self):
        self.assertIn("calibration", EVENT_KINDS)
        self.assertEqual(set(PAYLOAD_FIELDS), set(EVENT_KINDS))
        self.assertEqual(DEFAULT_TIER_BY_KIND["calibration"], "derived")
        # Adding a kind adds a key; it does not touch another kind's field
        # set, so no existing event's payload hash moves.  The calendar's own
        # set is C1's bridge's business and is only read here: since that
        # bridge landed it carries ``window`` and ``date_confidence``, which is
        # why ``window_of`` takes the emitter's own word when it is there.
        self.assertLessEqual(
            {"event_kind", "expected_date", "confirmed", "calendar_version_ref",
             "source_ref"},
            PAYLOAD_FIELDS["calendar"])
        self.assertLessEqual(
            {"window", "entry_ref", "date_confidence"},
            PAYLOAD_FIELDS["calendar"])
        self.assertNotIn("occurrence_ref", PAYLOAD_FIELDS["calendar"])

    def test_a_core_built_before_these_kinds_is_migrated_rather_than_broken(self):
        store = DaltonStore(":memory:")
        self.addCleanup(store.close)
        store.connection.executescript(
            """
            CREATE TABLE mission_deliverable_versions (
                version_id TEXT PRIMARY KEY,
                deliverable_ref TEXT NOT NULL,
                version_number INTEGER NOT NULL CHECK(version_number >= 1),
                prior_version_ref TEXT,
                mission_version_ref TEXT NOT NULL,
                mission_version_hash TEXT NOT NULL,
                playbook_version_ref TEXT NOT NULL,
                playbook_version_hash TEXT NOT NULL,
                kind TEXT NOT NULL CHECK(kind IN (
                    'industry_framework','initial_screen','industry_model','company_model',
                    'forecast_lines','investment_memo','weekly_brief','event_note'
                )),
                subject_ref TEXT NOT NULL,
                record_json TEXT NOT NULL,
                content_hash TEXT NOT NULL,
                actor_ref TEXT NOT NULL,
                created_at TEXT NOT NULL,
                UNIQUE(deliverable_ref, version_number)
            );
            """
        )
        MissionDeliverableAuthority(store)
        sql = store.connection.execute(
            "SELECT sql FROM sqlite_master WHERE name='mission_deliverable_versions'"
        ).fetchone()["sql"]
        for kind in DELIVERABLE_KINDS:
            self.assertIn(f"'{kind}'", sql)

    def test_the_two_model_purposes_are_registered(self):
        from dalton_core.cockpit_model import purposes

        self.assertIn("earnings_preview", purposes())
        self.assertIn("earnings_calibration", purposes())

    def test_both_purposes_are_brain_tier(self):
        from dalton_core.model_fallback_chain import TIER_BRAIN, tier_for

        self.assertEqual(tier_for("earnings_preview"), TIER_BRAIN)
        self.assertEqual(tier_for("earnings_calibration"), TIER_BRAIN)


if __name__ == "__main__":
    unittest.main()
