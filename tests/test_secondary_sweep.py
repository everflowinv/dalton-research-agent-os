"""P11s: a secondary allowance is spent on the documents its pass wants.

Live, both secondary allowances were unspendable.  They rode along on the
windows the prose pass had just drafted, and the prose pass drained all 30 of
its windows on the top-priority company's largest news pages: three consecutive
runs read 30 web-search windows, and neither the figures pass nor the discovery
pass reached a single filing or transcript.  So the sweep chooses its own
documents, and this is what it must get right.
"""

from __future__ import annotations

import unittest

from dalton_core.document_extraction_cli import _secondary_sweep


class FakeService:
    """Windows keyed by (review_id, offset); records what was asked."""

    def __init__(self, windows, answers):
        self.windows = windows
        self.answers = answers
        self.asked: list[tuple[str, int]] = []

    def view(self, *, review_id, expected_review_hash, offset, actor_ref,
             require_open=True):
        next_offset = self.windows[review_id][offset]
        return {"context": {"content_hash": f"{review_id}:{offset}",
                            "next_offset": next_offset}}

    def generate_numeric(self, *, review_id, offset, **_kwargs):
        self.asked.append((review_id, offset))
        answer = self.answers.get((review_id, offset), {})
        return {"status": "read", "verified": [], "refused": [], **answer}


def review(review_id, source_ref="source:alphaengine", document_ref=None):
    return {"review_id": review_id, "source_ref": source_ref,
            "document_ref": document_ref or f"doc:{review_id}"}


def summary():
    return {"numeric": [], "figures": 0}


COUNTS = {"verified": "verified", "refused": "refused"}


def sweep(service, reviews, out, *, limit, wanted=lambda r, s: True, specs=None):
    _secondary_sweep(
        service, [("automation:x", reviews, specs or {})], out,
        limit=limit, entries="numeric", wanted=wanted, call="generate_numeric",
        counts=COUNTS, total=("figures", "verified"),
    )


class SweepTests(unittest.TestCase):
    def test_it_reaches_a_document_the_prose_pass_never_got_to(self):
        # The whole point: the review is not the first in the order and was
        # never drafted, and the sweep still reads it.
        service = FakeService({"r1": {0: None}}, {})
        out = summary()
        sweep(service, [review("r1")], out, limit=3)
        self.assertEqual(service.asked, [("r1", 0)])
        self.assertEqual(len(out["numeric"]), 1)

    def test_a_review_this_pass_does_not_want_is_never_asked(self):
        service = FakeService({"r1": {0: None}, "r2": {0: None}}, {})
        out = summary()
        sweep(service, [review("r1", "source:web-search"), review("r2")], out,
              limit=3, wanted=lambda r, s: r["source_ref"] == "source:alphaengine")
        self.assertEqual(service.asked, [("r2", 0)])

    def test_the_gate_is_given_the_review_and_its_spec(self):
        seen = []
        service = FakeService({"r1": {0: None}}, {})

        def wanted(r, spec):
            seen.append((r["review_id"], spec))
            return False

        sweep(service, [review("r1", document_ref="doc:a")], summary(),
              limit=3, wanted=wanted, specs={"doc:a": "sell-side-reports"})
        self.assertEqual(seen, [("r1", "sell-side-reports")])

    def test_it_walks_a_document_window_by_window_until_the_allowance_runs_out(self):
        service = FakeService({"r1": {0: 100, 100: 200, 200: 300, 300: None}}, {})
        out = summary()
        sweep(service, [review("r1")], out, limit=2)
        self.assertEqual(service.asked, [("r1", 0), ("r1", 100)])

    def test_a_replayed_window_is_free_and_the_sweep_moves_on(self):
        # Otherwise a re-run spends its whole allowance re-reading the windows
        # the last run already paid for, and never advances.
        service = FakeService(
            {"r1": {0: 100, 100: 200, 200: None}},
            {("r1", 0): {"replayed": True}, ("r1", 100): {"replayed": True}},
        )
        out = summary()
        sweep(service, [review("r1")], out, limit=1)
        self.assertEqual(service.asked, [("r1", 0), ("r1", 100), ("r1", 200)])
        self.assertEqual([e["replayed"] for e in out["numeric"]], [True, True, False])

    def test_a_window_that_owes_nothing_costs_no_allowance(self):
        service = FakeService(
            {"r1": {0: 100, 100: None}}, {("r1", 0): {"status": "nothing_owed"}},
        )
        out = summary()
        sweep(service, [review("r1")], out, limit=1)
        self.assertEqual(service.asked, [("r1", 0), ("r1", 100)])

    def test_one_unreadable_window_costs_its_document_not_the_run(self):
        broken = FakeService({"r1": {0: None}, "r2": {0: None}}, {})
        original = broken.view

        def view(**kwargs):
            if kwargs["review_id"] == "r1":
                raise RuntimeError("manifest is missing")
            return original(**kwargs)

        broken.view = view
        out = summary()
        sweep(broken, [review("r1"), review("r2")], out, limit=3)
        self.assertEqual(broken.asked, [("r2", 0)])
        self.assertEqual(out["numeric"][0]["status"], "failed")
        self.assertIn("manifest is missing", out["numeric"][0]["reason"])

    def test_a_gated_window_ends_the_sweep_rather_than_repeating_the_gate(self):
        # No model is configured, so every other window is gated too; walking
        # them all would fill the summary with the same sentence.
        service = FakeService({"r1": {0: None}, "r2": {0: None}}, {("r1", 0): {"status": "gated"}})
        out = summary()
        sweep(service, [review("r1"), review("r2")], out, limit=5)
        self.assertEqual(service.asked, [("r1", 0)])
        self.assertEqual(out["numeric"], [])

    def test_an_allowance_of_zero_reads_nothing_at_all(self):
        service = FakeService({"r1": {0: None}}, {})
        sweep(service, [review("r1")], summary(), limit=0)
        self.assertEqual(service.asked, [])

    def test_the_total_counts_what_the_windows_produced(self):
        service = FakeService(
            {"r1": {0: 100, 100: None}},
            {("r1", 0): {"verified": [1, 2]}, ("r1", 100): {"verified": [3]}},
        )
        out = summary()
        sweep(service, [review("r1")], out, limit=5)
        self.assertEqual(out["figures"], 3)
        self.assertEqual([e["verified"] for e in out["numeric"]], [2, 1])


if __name__ == "__main__":
    unittest.main()
