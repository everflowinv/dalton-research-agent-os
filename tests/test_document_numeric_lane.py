"""P11r: the figures pass over a real bound window.

``test_document_numeric_extraction`` covers the request, the prompt and the
verification in isolation.  Nothing covered the pass actually running: the
first attempt to do so failed on a missing fixture guard, which means the
figures pass had never been executed through the routed worker at all.
"""

from __future__ import annotations

import tempfile
import unittest
import unittest.mock
from pathlib import Path

from dalton_core.document_figure_grade import FILED, SPOKEN
from tests.test_document_extraction import ExtractionHarness

# The harness document says this, over and over.
SENTENCE = "Management says client decisions remain cautious"


def response(*figures) -> dict:
    return {"schema_version": "0.1", "figures": list(figures)}


class NumericLaneTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.h = ExtractionHarness(Path(self.temp.name))
        self.addCleanup(self.h.close)

    def read(self, output):
        self.h.enable_fixture(output)
        return self.h.service.generate_numeric(
            **self.h.params, expected_context_hash=self.h.context()["content_hash"],
        )

    def quote_id(self):
        return self.h.context()["quotes"][0]["quote_id"]

    def test_a_gated_window_is_not_read_for_figures(self):
        result = self.h.service.generate_numeric(
            **self.h.params, expected_context_hash=self.h.context()["content_hash"],
        )
        self.assertEqual(result["status"], "gated")

    def test_a_figure_the_window_does_not_state_is_refused(self):
        # The floor is always owed, so this window is asked; the document
        # contains no revenue figure, and a model that invents one is refused
        # rather than believed.
        result = self.read(response({
            "quote_id": self.quote_id(), "metric_ref": "metric:revenue",
            "as_reported_label": "Net revenues", "value": "17.7", "unit": "currency",
            "currency": "USD", "period": "FY2026Q3", "basis": "gaap-reported",
            "scale": "billion",
        }))
        self.assertEqual(result["status"], "read")
        self.assertEqual(result["verified"], [])
        self.assertEqual(len(result["refused"]), 1)
        self.assertEqual(result["formal_authority_writes"], 0)

    def test_an_empty_answer_is_the_common_correct_one(self):
        result = self.read(response())
        self.assertEqual((result["status"], result["verified"], result["refused"]),
                         ("read", [], []))

    def test_the_answer_replays_rather_than_being_paid_for_twice(self):
        first = self.read(response())
        calls = self.h.adapter.calls
        again = self.h.service.generate_numeric(
            **self.h.params, expected_context_hash=self.h.context()["content_hash"],
        )
        self.assertEqual(self.h.adapter.calls, calls)
        # The answer is identical; only the flag that says it cost nothing
        # differs, and the lane spends its allowance on that flag.
        self.assertFalse(first["replayed"])
        self.assertTrue(again["replayed"])
        self.assertEqual({**again, "replayed": False}, first)

    def test_a_company_that_owes_nothing_costs_no_model_call(self):
        self.h.enable_fixture(response())
        with unittest.mock.patch.object(
            type(self.h.service), "numeric_slots", return_value=[]
        ):
            result = self.h.service.generate_numeric(
                **self.h.params, expected_context_hash=self.h.context()["content_hash"],
            )
        self.assertEqual(result["status"], "nothing_owed")
        self.assertEqual(self.h.adapter.calls, 0)

    def test_a_fixture_cannot_answer_where_the_broker_was_expected(self):
        # The first version of this test asserted only that running such an
        # order raised, and it passed for the wrong reason: the drift check
        # fires first, because a context carrying a model_binding is no longer
        # the context the window was bound from.  So the guard is exercised
        # where it actually lives.
        from dalton_core.document_numeric_extraction import build_work
        from dalton_core.metric_discovery_extraction import build_work as discovery_work
        from dalton_core.research_verification import ResearchVerificationError

        self.h.enable_fixture(response())
        context = self.h.context()
        slots = self.h.service.numeric_slots(context)
        worker = self.h.writer._document_extraction_worker_factory(
            self.h.service, context, self.h.params["actor_ref"],
        )
        for work in (build_work(context, slots), discovery_work(context)):
            # A fixture-mode context builds a fixture-mode order, which the
            # fixture worker accepts.
            self.assertEqual(work.metadata["execution_mode"], "hermetic_fixture")
            worker._before_model_call(work, None, None, False)
            claiming_broker = build_work(
                {**context, "model_binding": {"routing_policy_ref": "x"}}, slots,
            ) if work.metadata["task_ref"].startswith("task:document-numeric") else discovery_work(
                {**context, "model_binding": {"routing_policy_ref": "x"}},
            )
            self.assertEqual(claiming_broker.metadata["execution_mode"], "broker")
            with self.assertRaises(ResearchVerificationError) as caught:
                worker._before_model_call(claiming_broker, None, None, False)
            self.assertIn("impersonate broker", str(caught.exception))


class GradedFigureTests(unittest.TestCase):
    """P11v/P11w: what a figure is worth depends on what it was read out of."""

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.h = ExtractionHarness(Path(self.temp.name))
        self.addCleanup(self.h.close)

    def quote(self):
        return self.h.context()["quotes"][0]

    def figure(self, **overrides):
        # The harness document contains "client decisions" and, in the XSS
        # payload it fences, the digit 1 -- so both halves of the check pass
        # against text that is really there.
        base = {
            "quote_id": overrides.pop("quote_id", None) or self.quote()["quote_id"],
            "metric_ref": "metric:revenue",
            "as_reported_label": "client decisions",
            "value": "1", "unit": "count", "currency": None,
            "period": "FY2026Q3", "basis": "management-reported", "scale": None,
        }
        base.update(overrides)
        return base

    def read(self, output, **kwargs):
        self.h.enable_fixture(output)
        return self.h.service.generate_numeric(
            **self.h.params, expected_context_hash=self.h.context()["content_hash"],
            **kwargs,
        )

    def test_a_transcript_figure_is_stored_and_says_it_was_spoken(self):
        # The harness document is an earnings-call transcript, so this is the
        # grade the lane derives without being told.
        result = self.read(response(self.figure()))
        self.assertEqual(result["status"], "read")
        self.assertEqual(result["source_grade"], SPOKEN)
        self.assertEqual(result["recorded"], ["metric:revenue"])
        [held] = self.h.missions.document_figures(self.h.review["company_ref"])
        self.assertEqual(held["source_grade"], SPOKEN)
        self.assertEqual(held["value"], "1")

    def test_a_filed_figure_is_stored_under_the_other_grade(self):
        result = self.read(response(self.figure()), source_grade=FILED)
        self.assertEqual(result["source_grade"], FILED)
        [held] = self.h.missions.document_figures(
            self.h.review["company_ref"], source_grade=FILED)
        self.assertEqual(held["metric_ref"], "metric:revenue")

    def test_a_figure_the_window_does_not_contain_is_never_stored(self):
        result = self.read(response(self.figure(value="999999")))
        self.assertEqual(result["verified"], [])
        self.assertEqual(result["recorded"], [])
        self.assertEqual(self.h.missions.document_figures(self.h.review["company_ref"]), [])

    def test_a_closed_review_is_still_read_for_figures(self):
        # The prose pass drafts 30 windows a tick and closes the review; this
        # pass reads 10. On the open queue alone it is lapped and locked out of
        # every document before it has finished one -- which is what happened
        # to a live 10-K, closed after two ticks with 32 windows never read.
        from dalton_core.store import content_hash

        self.h.missions.resolve_document_review(
            self.h.review["review_id"], resolution="dismissed",
            actor_ref=self.h.params["actor_ref"],
            rationale="fixture: prose found nothing admissible, as in a table-heavy filing",
        )
        closed = self.h.missions.document_review(self.h.review["review_id"])
        self.assertEqual(closed["state"], "dismissed")
        params = {**self.h.params, "expected_review_hash": content_hash(closed)}
        context = self.h.service.view(**params, require_open=False)["context"]
        self.h.enable_fixture(
            response(self.figure(quote_id=context["quotes"][0]["quote_id"])),
            expected_review_hash=params["expected_review_hash"], require_open=False,
        )
        result = self.h.service.generate_numeric(
            **params, expected_context_hash=context["content_hash"],
        )
        self.assertEqual(result["status"], "read")
        self.assertEqual(result["recorded"], ["metric:revenue"])

    def test_a_document_kind_with_no_grade_is_not_read_for_figures_at_all(self):
        # Sell-side research quotes numbers constantly and some of them are the
        # analyst's estimate; the pass must not spend a call on one.
        self.h.enable_fixture(response())
        with unittest.mock.patch.object(
            type(self.h.service), "_document_spec_ref", return_value="sell-side-reports",
        ):
            result = self.h.service.generate_numeric(
                **self.h.params, expected_context_hash=self.h.context()["content_hash"],
            )
        self.assertEqual(result["status"], "not_graded")
        self.assertEqual(self.h.adapter.calls, 0)


if __name__ == "__main__":
    unittest.main()
