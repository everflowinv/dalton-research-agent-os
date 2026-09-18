"""C2-3: a document a secondary pass has finished with leaves the queue.

The live shape this was written against -- the "美国 Hyperscaler 研究" workspace,
2026-09-18, 05:43 to 07:33 UTC, twenty-three consecutive runs:

  * the figures pass walked 107 windows over the same four reviews every five
    minutes, 106 of them ``replayed: true`` with ``recorded: 0`` and
    ``verified: 0``, ``numeric_fresh: 0``;
  * the 107th was an AlphaEngine document that answered ``not_attributed`` --
    the document names no company this lane covers -- and was re-derived on
    every single tick to answer that again;
  * ``reviews_scanned`` climbed 54 -> 118 while ``reviews_complete`` sat at 26
    and the cockpit's read total sat at 54;
  * model-call volume in the workspace fell from 339/hour to 7/hour.

Replay costs no model call, which is the point of the 2026-09-16 de-duplication
and is kept exactly.  What it does cost is the document: every one of those 107
windows re-fetched, re-rendered and re-hashed its source to recover an answer
already on disk.  A review whose every window replays and records nothing is
finished, and it has to say so and get out of the way.

The legacy install shows the same pathology one size larger: 87 windows
answering ``not_attributed`` and 214 replayed reads in a single tick, against a
queue of 156 reviews of which 139 cannot be viewed at all.
"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from dalton_core.document_extraction_cli import _secondary_sweep
from dalton_core.document_extraction_windows import (
    ExtractionWindowError,
    ExtractionWindowLedger,
)
from dalton_core.document_numeric_claim import (
    NumericCandidateError,
    verify_numeric_candidate,
)
from dalton_core.store import content_hash

HASH = "a" * 64
OTHER_HASH = "b" * 64


class Clock:
    def __init__(self, moment: datetime) -> None:
        self.moment = moment

    def __call__(self) -> datetime:
        return self.moment


class FakeService:
    """Windows keyed by (review_id, offset); records every context derived.

    ``viewed`` is the expensive half: deriving a window's context is what
    re-fetches and re-renders the whole document, and it happens whether or not
    the answer that follows it is replayed for free.
    """

    def __init__(self, windows, answers):
        self.windows = windows
        self.answers = answers
        self.asked: list[tuple[str, int]] = []
        self.viewed: list[tuple[str, int]] = []

    def view(self, *, review_id, expected_review_hash, offset, actor_ref,
             require_open=True):
        self.viewed.append((review_id, offset))
        next_offset = self.windows[review_id][offset]
        return {"context": {"content_hash": f"{review_id}:{offset}",
                            "next_offset": next_offset}}

    def generate_numeric(self, *, review_id, offset, **_kwargs):
        self.asked.append((review_id, offset))
        answer = self.answers.get((review_id, offset), {})
        return {"status": "read", "verified": [], "refused": [], "recorded": [],
                **answer}


def review(review_id, source_ref="source:sec-edgar", document_ref=None):
    return {"review_id": review_id, "source_ref": source_ref,
            "company_ref": "company:ticker:amzn",
            "document_ref": document_ref or f"doc:{review_id}"}


def summary():
    return {"numeric": [], "figures": 0, "numeric_fresh": 0,
            "exhausted_reviews": [], "advanced_to": {}}


COUNTS = {"verified": "verified", "refused": "refused", "recorded": "recorded"}


class ExhaustionTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.connection = sqlite3.connect(str(Path(self.temp.name) / "core.sqlite"))
        self.connection.row_factory = sqlite3.Row
        self.addCleanup(self.connection.close)
        self.clock = Clock(datetime(2026, 9, 18, 7, 33, tzinfo=timezone.utc))
        self.ledger = ExtractionWindowLedger(self.connection, clock=self.clock)

    def sweep(self, service, reviews, out, *, limit=10, entries="numeric",
              ledger=True, model_config_hash=None):
        return _secondary_sweep(
            service, [("automation:extraction", reviews, {})], out,
            limit=limit, entries=entries, wanted=lambda r, s: True,
            call="generate_numeric", counts=COUNTS,
            total=("figures", "recorded"), spent_key="numeric_fresh",
            ledger=self.ledger if ledger else None,
            model_config_hash=model_config_hash,
        )


class QueueAdvanceTests(ExhaustionTestCase):
    """The stall itself: four finished documents in front of 114 unread ones."""

    def _stalled_queue(self):
        # ``done`` is the live shape: every window replayed, nothing recorded.
        # ``fresh`` is one of the 114 documents nobody has read.
        service = FakeService(
            {"done": {0: 12000, 12000: 24000, 24000: None}, "fresh": {0: None}},
            {("done", 0): {"replayed": True}, ("done", 12000): {"replayed": True},
             ("done", 24000): {"replayed": True}},
        )
        return service, [review("done"), review("fresh")]

    def test_the_never_read_document_is_reached_and_the_finished_one_is_marked(self):
        service, reviews = self._stalled_queue()
        out = summary()
        self.sweep(service, reviews, out)
        self.assertEqual(service.asked[-1], ("fresh", 0))
        self.assertEqual(out["numeric_fresh"], 1)
        self.assertEqual([e["review_id"] for e in out["exhausted_reviews"]], ["done"])
        marked = out["exhausted_reviews"][0]
        self.assertEqual(marked["reason"], "windows_exhausted")
        self.assertEqual(marked["windows"], 3)
        self.assertIn("replayed", marked["detail"])

    def test_the_next_tick_does_not_touch_the_finished_document_at_all(self):
        # This is the two hours of work the lane was paying for: 107 window
        # contexts re-derived every five minutes to recover answers on disk.
        service, reviews = self._stalled_queue()
        self.sweep(service, reviews, summary())
        second = FakeService(service.windows, service.answers)
        out = summary()
        self.sweep(second, reviews, out)
        self.assertEqual(second.viewed, [("fresh", 0)])
        self.assertEqual(second.asked, [("fresh", 0)])
        self.assertEqual(out["exhausted_reviews"], [])
        self.assertEqual(out["advanced_to"]["numeric"]["reviews_skipped_exhausted"], 1)

    def test_a_review_that_recorded_something_is_never_called_finished(self):
        # Replay that still yields a record is a document with more to give.
        service = FakeService(
            {"done": {0: 12000, 12000: None}},
            {("done", 0): {"replayed": True, "recorded": [{"figure": 1}]},
             ("done", 12000): {"replayed": True}},
        )
        out = summary()
        self.sweep(service, [review("done")], out)
        self.assertEqual(out["exhausted_reviews"], [])
        self.assertEqual(out["figures"], 1)

    def test_a_review_whose_walk_was_cut_short_is_not_called_finished(self):
        # The allowance ran out mid-document, so nobody has seen its last
        # window; calling it exhausted would lose the rest of the filing.
        service = FakeService(
            {"done": {0: 12000, 12000: 24000, 24000: None}},
            {("done", 0): {"replayed": True}},
        )
        out = summary()
        self.sweep(service, [review("done")], out, limit=1)
        self.assertEqual(out["exhausted_reviews"], [])

    def test_a_window_that_could_not_be_viewed_does_not_finish_its_review(self):
        service = FakeService({"done": {0: 12000, 12000: None}},
                              {("done", 0): {"replayed": True}})
        original = service.view

        def view(**kwargs):
            if kwargs["offset"] == 12000:
                raise RuntimeError("manifest is missing")
            return original(**kwargs)

        service.view = view
        out = summary()
        self.sweep(service, [review("done")], out)
        self.assertEqual(out["exhausted_reviews"], [])


class NotAttributedTests(ExhaustionTestCase):
    """P13c attribution is a fact about the document, so it is paid for once."""

    def test_it_is_recorded_once_and_the_rest_of_the_document_is_not_walked(self):
        service = FakeService(
            {"alpha": {0: 12000, 12000: 24000, 24000: None}},
            {("alpha", 0): {"status": "not_attributed"}},
        )
        out = summary()
        self.sweep(service, [review("alpha", source_ref="source:alphaengine")], out)
        self.assertEqual(service.viewed, [("alpha", 0)])
        self.assertEqual(out["exhausted_reviews"][0]["reason"], "not_attributed")
        self.assertEqual(out["numeric_fresh"], 0)

    def test_the_next_tick_pays_nothing_for_it_at_all(self):
        windows = {"alpha": {0: None}}
        answers = {("alpha", 0): {"status": "not_attributed"}}
        first = FakeService(windows, answers)
        reviews = [review("alpha", source_ref="source:alphaengine")]
        self.sweep(first, reviews, summary())
        second = FakeService(windows, answers)
        out = summary()
        self.sweep(second, reviews, out)
        self.assertEqual(second.viewed, [])
        self.assertEqual(second.asked, [])
        self.assertEqual(out["numeric"], [])

    def test_the_other_pass_may_still_read_a_document_this_one_finished(self):
        # A document the figures pass is done with can still tell the
        # discovery pass which measures the market uses.
        windows = {"d": {0: None}}
        first = FakeService(windows, {("d", 0): {"replayed": True}})
        self.sweep(first, [review("d")], summary(), entries="numeric")
        second = FakeService(windows, {("d", 0): {"replayed": True}})
        out = {"discovery": [], "metrics_observed": 0, "discovery_fresh": 0}
        _secondary_sweep(
            second, [("automation:extraction", [review("d")], {})], out,
            limit=10, entries="discovery", wanted=lambda r, s: True,
            call="generate_numeric", counts=COUNTS,
            total=("metrics_observed", "recorded"), spent_key="discovery_fresh",
            ledger=self.ledger,
        )
        self.assertEqual(second.asked, [("d", 0)])


class SummaryCounterTests(ExhaustionTestCase):
    """What the owner reads to see whether the queue moved."""

    def test_advanced_to_names_the_first_window_this_pass_paid_for(self):
        service = FakeService({"done": {0: None}, "fresh": {0: 12000, 12000: None}},
                              {("done", 0): {"replayed": True}})
        out = summary()
        self.sweep(service, [review("done"), review("fresh")], out)
        advanced = out["advanced_to"]["numeric"]
        self.assertEqual(advanced["review_id"], "fresh")
        self.assertEqual(advanced["document_ref"], "doc:fresh")
        self.assertEqual(advanced["offset"], 0)
        self.assertEqual(advanced["fresh_windows"], 2)

    def test_a_pass_that_paid_for_nothing_says_so_rather_than_being_silent(self):
        service = FakeService({"done": {0: None}}, {("done", 0): {"replayed": True}})
        out = summary()
        self.sweep(service, [review("done")], out)
        self.assertEqual(out["advanced_to"]["numeric"]["review_id"], None)
        self.assertEqual(out["advanced_to"]["numeric"]["fresh_windows"], 0)

    def test_refused_candidates_are_named_when_nothing_was_recorded(self):
        # ``recorded: 0`` with a refusal behind it is a contract failure, not
        # an empty document, and the summary used to report only the zero.
        service = FakeService(
            {"r": {0: None}},
            {("r", 0): {"refused": [{"reason": "value is not a decimal number"},
                                    {"reason": "value is not a decimal number"}]}},
        )
        out = summary()
        self.sweep(service, [review("r")], out)
        self.assertEqual(out["numeric"][0]["refusal_reasons"],
                         ["value is not a decimal number"])

    def test_a_window_that_recorded_something_is_not_reported_as_refused(self):
        service = FakeService(
            {"r": {0: None}},
            {("r", 0): {"recorded": [{"figure": 1}],
                        "refused": [{"reason": "cites a quote that was not supplied"}]}},
        )
        out = summary()
        self.sweep(service, [review("r")], out)
        self.assertNotIn("refusal_reasons", out["numeric"][0])

    def test_the_sweep_still_runs_without_a_ledger(self):
        # A caller with no ledger reports the verdict and forgets it, rather
        # than failing; nothing about the tick depends on the write.
        service = FakeService({"done": {0: None}}, {("done", 0): {"replayed": True}})
        out = summary()
        self.sweep(service, [review("done")], out, ledger=False)
        self.assertEqual(out["exhausted_reviews"][0]["reason"], "windows_exhausted")


class ExhaustionLedgerTests(ExhaustionTestCase):
    """The verdict is scoped to the bytes and the model that read them."""

    def _exhaust(self, **overrides):
        payload = {
            "review_id": "mission-document-review:one", "source_review_hash": HASH,
            "pass_ref": "numeric", "reason": "windows_exhausted",
            "detail": "all 27 windows replayed and recorded nothing", "windows": 27,
        }
        payload.update(overrides)
        return self.ledger.exhaust(**payload)

    def test_it_is_written_once_and_re_stated_rather_than_duplicated(self):
        self._exhaust()
        again = self._exhaust()
        self.assertEqual(again["hit_count"], 2)
        rows = self.connection.execute(
            "SELECT COUNT(*) FROM document_extraction_review_exhaustion").fetchone()
        self.assertEqual(rows[0], 1)

    def test_a_re_acquired_review_is_read_again_from_scratch(self):
        self._exhaust()
        active = self.ledger.exhausted_reviews("numeric")
        self.assertIn(("mission-document-review:one", HASH), active)
        self.assertNotIn(("mission-document-review:one", OTHER_HASH), active)

    def test_swapping_the_model_re_opens_the_document(self):
        self._exhaust(model_config_hash="config-one")
        self.assertEqual(
            self.ledger.exhausted_reviews("numeric", model_config_hash="config-two"), {})
        self.assertEqual(
            len(self.ledger.exhausted_reviews("numeric", model_config_hash="config-one")), 1)

    def test_a_caller_that_names_no_model_inherits_every_verdict(self):
        # The conservative reading: it reads less, never more.
        self._exhaust(model_config_hash="config-one")
        self.assertEqual(len(self.ledger.exhausted_reviews("numeric")), 1)

    def test_each_pass_keeps_its_own_verdict(self):
        self._exhaust()
        self.assertEqual(self.ledger.exhausted_reviews("discovery"), {})

    def test_an_invented_reason_is_refused(self):
        with self.assertRaises(ExtractionWindowError):
            self._exhaust(reason="looked_boring")

    def test_a_review_hash_that_is_not_a_digest_is_refused(self):
        with self.assertRaises(ExtractionWindowError):
            self._exhaust(source_review_hash="not-a-hash")

    def test_a_verdict_cannot_be_deleted(self):
        self._exhaust()
        with self.assertRaises(sqlite3.DatabaseError):
            self.connection.execute(
                "DELETE FROM document_extraction_review_exhaustion")

    def test_a_verdict_cannot_be_written_without_the_ledger(self):
        with self.assertRaises(sqlite3.DatabaseError):
            self.connection.execute(
                "INSERT INTO document_extraction_review_exhaustion("
                "exhaustion_id,review_id,source_review_hash,pass_ref,reason,detail,"
                "created_at,updated_at) VALUES('x','r',?, 'numeric','not_attributed',"
                "'forged','2026-09-18T00:00:00+00:00','2026-09-18T00:00:00+00:00')",
                (HASH,))


class ReportedNumberTests(unittest.TestCase):
    """P11w: a figure written the way the filing writes it is still a number.

    Live, all twenty figures the three 10-Ks produced under the current model
    configuration were refused with "value is not a decimal number", because
    the prompt asks for the number exactly as the document writes it and the
    document writes "716,924".  The citation check already folded the
    separators away on the quote's side; the candidate's side did not.
    """

    QUOTE = ("Total net sales were $716,924 million for the year ended "
             "December 31, 2025.")

    def candidate(self, value):
        return {
            "quote_id": "quote:1", "metric_ref": "metric:revenue",
            "subject_as_named": "AMAZON.COM, INC.",
            "as_reported_label": "Total net sales", "value": value,
            "unit": "currency", "currency": "USD",
            "period": "Year Ended December 31, 2025",
            "basis": "gaap-reported", "scale": "million",
        }

    def test_a_grouped_number_verifies_against_the_citation_it_came_from(self):
        verified = verify_numeric_candidate(
            self.candidate("716,924"), {"quote:1": self.QUOTE})
        self.assertEqual(verified["value"], "716924")

    def test_the_ungrouped_form_is_the_same_figure(self):
        self.assertEqual(
            verify_numeric_candidate(
                self.candidate("716924"), {"quote:1": self.QUOTE})["content_hash"],
            verify_numeric_candidate(
                self.candidate("716,924"), {"quote:1": self.QUOTE})["content_hash"],
        )

    def test_a_decimal_comma_is_still_not_a_number_this_lane_reads(self):
        # "1,2" is two digits joined by a comma, not a thousands group, and
        # guessing which it meant is how a lane invents a figure.
        with self.assertRaises(NumericCandidateError):
            verify_numeric_candidate(self.candidate("1,2"), {"quote:1": self.QUOTE})

    def test_a_grouped_number_the_citation_does_not_contain_is_still_refused(self):
        with self.assertRaises(NumericCandidateError):
            verify_numeric_candidate(
                self.candidate("716,925"), {"quote:1": self.QUOTE})


if __name__ == "__main__":
    unittest.main()
