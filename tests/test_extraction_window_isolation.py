"""C2-1/C2-2: per-window isolation, window de-duplication, reading order, batch size.

The live shape these were written against (2026-09-16, 48h):

  * 432 extraction runs, 411 of them ``failed`` -- 176 on
    ``systemic_model_failure`` and 202 on ``all queued document views failed``;
  * 30,372 window reads producing 517 records and 8 figures, with 24% of the
    reads being *replays of windows that already held a terminal failure* and
    could therefore never succeed;
  * of 991 recorded window failures, 565 never reserved a micro and 66 more had
    already settled their exact cost -- i.e. two thirds of the failures that
    cancelled the rest of the batch cost nothing to walk past;
  * the awaiting count pinned at 192 for an hour at a time, because the
    one-hour idle hold was applied to a queue that was not idle;
  * 60% of 415 read proofs taken from ``management-changes`` news pages while
    five 10-Ks and fifty earnings-call transcripts sat acquired and unread;
  * 246 of the day's 2,000 authorised document reads spent.
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from dalton_core.document_extraction import DocumentExtractionService
from dalton_core.document_extraction_cli import (
    CONSECUTIVE_CERTAIN_FAILURE_LIMIT,
    HIGH_PRIORITY_WINDOW_SHARE,
    is_high_priority_spec,
    run_extraction,
    spec_reading_rank,
)
from dalton_core.document_extraction_windows import (
    ExtractionWindowError,
    ExtractionWindowLedger,
    classify_window_failure,
    replayed_window_failure,
    windows_for_tick,
)
from dalton_core.store import authorization_flag

from tests.test_document_extraction_automation import AutomationAdmissionTests

HASH = "a" * 64


class WindowFailureClassificationTests(unittest.TestCase):
    """Which failures are settled facts about one window, and which are not."""

    def test_a_window_that_never_reserved_a_micro_cost_nothing(self) -> None:
        verdict = classify_window_failure(error_code=None, budget_status="not_reserved")
        self.assertTrue(verdict["cost_certain"])
        self.assertEqual(verdict["failure_class"], "permanent")

    def test_a_settled_contract_refusal_is_a_fact_about_this_window(self) -> None:
        verdict = classify_window_failure(
            error_code="MODEL_OUTPUT_CONTRACT_REJECTED", budget_status="settled")
        self.assertTrue(verdict["cost_certain"])
        self.assertEqual(verdict["failure_class"], "permanent")

    def test_a_spent_day_budget_is_about_the_day_not_the_bytes(self) -> None:
        verdict = classify_window_failure(
            error_code="MODEL_ADAPTER_REJECTED", budget_status="rejected")
        self.assertTrue(verdict["cost_certain"])
        self.assertEqual(verdict["failure_class"], "deferred")

    def test_an_open_reservation_is_never_isolated(self) -> None:
        # The 2026-09-14 discipline, unchanged: the lane does not know whether
        # the provider was paid, so the batch ends and nothing is retried.
        verdict = classify_window_failure(error_code="TIMEOUT", budget_status="reserved")
        self.assertFalse(verdict["cost_certain"])
        self.assertIsNone(verdict["failure_class"])
        self.assertIn("has not settled", verdict["reason"])

    def test_an_explicit_unknown_outranks_a_settled_ledger(self) -> None:
        verdict = classify_window_failure(
            error_code="POST_SEND_RESULT_UNKNOWN", budget_status="settled")
        self.assertFalse(verdict["cost_certain"])

    def test_a_missing_budget_status_is_unknown_rather_than_free(self) -> None:
        self.assertFalse(classify_window_failure(
            error_code=None, budget_status=None)["cost_certain"])

    def test_a_cached_terminal_result_sent_nothing_so_nothing_is_unknown(self) -> None:
        verdict = replayed_window_failure(error_code="BUSY")
        self.assertTrue(verdict["cost_certain"])
        self.assertEqual(verdict["failure_class"], "permanent")
        self.assertEqual(verdict["budget_status"], "replayed")


class Clock:
    def __init__(self, moment: datetime) -> None:
        self.moment = moment

    def __call__(self) -> datetime:
        return self.moment


class WindowLedgerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.connection = sqlite3.connect(str(Path(self.temp.name) / "core.sqlite"))
        self.connection.row_factory = sqlite3.Row
        self.addCleanup(self.connection.close)
        self.clock = Clock(datetime(2026, 9, 16, 12, 0, tzinfo=timezone.utc))
        self.ledger = ExtractionWindowLedger(self.connection, clock=self.clock)

    def _exclude(self, **overrides):
        payload = {
            "review_id": "mission-document-review:one", "source_review_hash": HASH,
            "offset": 0, "next_offset": 12000,
            "classification": classify_window_failure(
                error_code="MODEL_ADAPTER_REJECTED", budget_status="not_reserved"),
        }
        payload.update(overrides)
        return self.ledger.exclude(**payload)

    def test_a_dead_window_is_written_down_once_with_where_to_continue(self) -> None:
        record = self._exclude()
        self.assertEqual(record["failure_class"], "permanent")
        self.assertEqual(record["next_offset"], 12000)
        self.assertEqual(record["hit_count"], 1)
        active = self.ledger.exclusions("mission-document-review:one", HASH)
        self.assertEqual(set(active), {0})

    def test_walking_past_it_again_bumps_the_count_not_the_verdict(self) -> None:
        self._exclude()
        again = self._exclude()
        self.assertEqual(again["hit_count"], 2)
        self.assertEqual(self.ledger.count_active(), 1)

    def test_a_different_review_revision_is_read_from_scratch(self) -> None:
        # A re-acquired document is different bytes; a verdict about the old
        # ones must not silently cover the new ones.
        self._exclude()
        self.assertEqual(self.ledger.exclusions("mission-document-review:one", "b" * 64), {})

    def test_swapping_the_model_re_opens_a_window_the_old_one_could_not_answer(self) -> None:
        # "This window's output could not be parsed" is a fact about one model.
        self._exclude(model_config_hash="m" * 64)
        self.assertEqual(
            set(self.ledger.exclusions("mission-document-review:one", HASH,
                                       model_config_hash="m" * 64)),
            {0},
        )
        self.assertEqual(
            self.ledger.exclusions("mission-document-review:one", HASH,
                                   model_config_hash="n" * 64),
            {},
        )
        # A caller that does not know its configuration gets the conservative
        # reading: every exclusion still applies.
        self.assertEqual(
            set(self.ledger.exclusions("mission-document-review:one", HASH)), {0})

    def test_a_day_budget_refusal_lapses_when_the_day_rolls_over(self) -> None:
        self._exclude(classification=classify_window_failure(
            error_code=None, budget_status="rejected"))
        self.assertEqual(set(self.ledger.exclusions("mission-document-review:one", HASH)), {0})
        self.clock.moment = self.clock.moment + timedelta(days=1)
        self.assertEqual(self.ledger.exclusions("mission-document-review:one", HASH), {})

    def test_a_window_whose_cost_is_unknown_may_not_be_excluded(self) -> None:
        with self.assertRaises(ExtractionWindowError):
            self._exclude(classification=classify_window_failure(
                error_code="TIMEOUT", budget_status="reserved"))

    def test_a_window_that_recorded_something_is_evidence_not_an_exclusion(self) -> None:
        with self.assertRaises(ExtractionWindowError):
            self._exclude(recorded=3)

    def test_the_table_refuses_an_unauthorised_writer(self) -> None:
        flag = authorization_flag(
            self.connection, "dalton_document_extraction_window_authorized")
        flag.authorized = False
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute(
                "INSERT INTO document_extraction_window_exclusions("
                "exclusion_id,review_id,source_review_hash,window_offset,failure_class,"
                "reason,created_at,updated_at) VALUES('x','r',?,0,'permanent','r','t','t')",
                (HASH,),
            )

    def test_an_exclusion_can_never_be_deleted(self) -> None:
        self._exclude()
        with self.assertRaises(sqlite3.IntegrityError):
            self.connection.execute("DELETE FROM document_extraction_window_exclusions")


class BatchSizeTests(unittest.TestCase):
    """The ceiling is the owner's; the plan is the quota that is left."""

    def test_a_barely_touched_quota_spends_the_configured_ceiling(self) -> None:
        # The live number: 2,000 reads authorised, 246 spent, 30 configured.
        budget = windows_for_tick(
            configured=30, awaiting=192, daily_cap=2000, used_today=246,
            ticks_remaining=120)
        self.assertEqual(budget["windows"], 30)
        self.assertEqual(budget["bound_by"], "configured")

    def test_a_nearly_spent_quota_is_not_burned_in_one_tick(self) -> None:
        budget = windows_for_tick(
            configured=30, awaiting=192, daily_cap=2000, used_today=1993,
            ticks_remaining=20)
        self.assertEqual(budget["windows"], 7)
        self.assertEqual(budget["bound_by"], "daily_quota")

    def test_a_spent_quota_still_reads_one_window_rather_than_none(self) -> None:
        budget = windows_for_tick(
            configured=30, awaiting=5, daily_cap=100, used_today=100, ticks_remaining=10)
        self.assertEqual((budget["windows"], budget["bound_by"]), (1, "daily_quota_spent"))

    def test_a_mission_without_a_cap_uses_what_it_was_configured_for(self) -> None:
        budget = windows_for_tick(
            configured=12, awaiting=3, daily_cap=None, used_today=0, ticks_remaining=50)
        self.assertEqual((budget["windows"], budget["bound_by"]), (12, "configured"))

    def test_the_ceiling_is_never_exceeded_however_much_quota_is_left(self) -> None:
        budget = windows_for_tick(
            configured=4, awaiting=900, daily_cap=100000, used_today=0, ticks_remaining=1)
        self.assertEqual(budget["windows"], 4)

    def test_the_arithmetic_is_refused_rather_than_guessed(self) -> None:
        for kwargs in (
            {"configured": 0, "awaiting": 1, "daily_cap": 10, "used_today": 0, "ticks_remaining": 1},
            {"configured": 4, "awaiting": 1, "daily_cap": 10, "used_today": 0, "ticks_remaining": 0},
            {"configured": 4, "awaiting": -1, "daily_cap": 10, "used_today": 0, "ticks_remaining": 1},
            {"configured": 4, "awaiting": 1, "daily_cap": -2, "used_today": 0, "ticks_remaining": 1},
        ):
            with self.assertRaises(ExtractionWindowError):
                windows_for_tick(**kwargs)


class ReadingOrderTests(unittest.TestCase):
    """The README contract: the call and the filing first, the news wire last."""

    def test_the_owner_ladder_decides_the_rank(self) -> None:
        ladder = [
            "annual-report-10k", "earnings-call-transcripts", "sell-side-reports",
            "expert-network-transcripts", "sales-notes", "industry-demand",
            "management-changes",
        ]
        ranks = [spec_reading_rank(spec) for spec in ladder]
        self.assertEqual(ranks, sorted(ranks))
        self.assertLess(spec_reading_rank("annual-report-10k"),
                        spec_reading_rank("management-changes"))

    def test_competitive_landscape_is_industry_not_news(self) -> None:
        self.assertEqual(spec_reading_rank("competitive-landscape"),
                         spec_reading_rank("industry-demand"))

    def test_a_spec_nobody_mapped_sorts_last_rather_than_being_promoted(self) -> None:
        self.assertGreater(spec_reading_rank("client-demand-and-budgets"),
                           spec_reading_rank("management-changes"))

    def test_the_reserved_share_protects_filings_calls_and_covering_brokers(self) -> None:
        for spec in ("annual-report-10k", "company-press-release",
                     "earnings-call-transcripts", "sell-side-reports"):
            self.assertTrue(is_high_priority_spec(spec), spec)
        for spec in ("management-changes", "industry-demand", "competitive-landscape",
                     "sales-notes", None):
            self.assertFalse(is_high_priority_spec(spec), spec)

    def test_half_a_tick_is_reserved_so_news_cannot_take_the_whole_batch(self) -> None:
        self.assertEqual(int(30 * HIGH_PRIORITY_WINDOW_SHARE), 15)


class WindowIsolationRunTests(AutomationAdmissionTests):
    """The run-level behaviour, over the same harness the lane's own tests use."""

    def test_a_settled_failure_isolates_one_window_and_the_run_reads_on(self) -> None:
        summary = self._run_with_generation_results([
            {"status": "failed", "suggestions": [], "error_code": "MODEL_ADAPTER_REJECTED",
             "work_order_ref": "work:document-extraction:refused",
             "model_budget": {"status": "not_reserved"}},
            {"status": "succeeded", "suggestions": [],
             "work_order_ref": "work:document-extraction:ok"},
        ])
        # The second window was still read: that is the whole change.
        self.assertEqual(len(summary["drafted"]), 2)
        self.assertEqual(summary["drafted"][1]["status"], "succeeded")
        self.assertEqual(len(summary["isolated_windows"]), 1)
        self.assertEqual(summary["isolated_windows"][0]["failure_class"], "permanent")
        self.assertNotEqual(summary["stop_reason"], "systemic_model_failure")
        self.assertTrue(summary["partial"]["productive"])
        self.assertEqual(summary["partial"]["windows_succeeded"], 1)
        # The review still cannot complete: one of its windows was never read.
        self.assertEqual(summary["reviews_complete"], 0)
        self.assertEqual(summary["resolved_reviews"], [])

    def test_an_unknown_send_still_ends_the_batch_with_its_reservation_open(self) -> None:
        summary = self._run_with_generation_results([
            {"status": "failed", "suggestions": [], "error_code": "POST_SEND_RESULT_UNKNOWN",
             "work_order_ref": "work:document-extraction:unknown",
             "model_budget": {"status": "reserved"}},
        ])
        self.assertEqual(summary["stop_reason"], "systemic_model_failure")
        self.assertEqual(summary["status"], "failed")
        self.assertEqual(len(summary["drafted"]), 1)
        # Nothing was written down about it: an unknown send is not a verdict
        # about the window, so the window keeps its place in the queue.
        self.assertEqual(summary["isolated_windows"], [])
        self.assertFalse(summary["blocked_window"]["cost_certain"])

    def test_a_reserved_but_unsettled_timeout_is_treated_as_unknown(self) -> None:
        summary = self._run_with_generation_results([
            {"status": "failed", "suggestions": [], "error_code": "TIMEOUT",
             "work_order_ref": "work:document-extraction:timeout",
             "model_budget": {"status": "reserved"}},
        ])
        self.assertEqual(summary["stop_reason"], "systemic_model_failure")
        self.assertEqual(summary["isolated_windows"], [])

    def test_an_outage_still_stops_the_run_rather_than_being_isolated_forever(self) -> None:
        # Isolating one settled failure is safe; reading into an outage one
        # window at a time is the old mistake with extra steps.  The limit is
        # lowered here so a two-window fixture can reach it.
        with patch(
            "dalton_core.document_extraction_cli.CONSECUTIVE_CERTAIN_FAILURE_LIMIT", 1
        ):
            summary = self._run_with_generation_results([
                {"status": "failed", "suggestions": [], "error_code": "MODEL_ADAPTER_REJECTED",
                 "work_order_ref": f"work:document-extraction:refused-{index}",
                 "model_budget": {"status": "not_reserved"}}
                for index in range(4)
            ])
        self.assertEqual(summary["stop_reason"], "systemic_model_failure")
        self.assertEqual(len(summary["drafted"]), 1)
        self.assertIn("in a row", summary["blocked_window"]["reason"])
        self.assertGreaterEqual(CONSECUTIVE_CERTAIN_FAILURE_LIMIT, 2)

    def test_the_summary_says_what_the_run_got_done_whatever_stopped_it(self) -> None:
        summary = self._run_with_generation_results([
            {"status": "failed", "suggestions": [], "error_code": "TIMEOUT",
             "work_order_ref": "work:document-extraction:timeout",
             "model_budget": {"status": "reserved"}},
        ])
        self.assertIn("partial", summary)
        self.assertEqual(summary["partial"]["windows_succeeded"], 0)
        self.assertFalse(summary["partial"]["productive"])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
