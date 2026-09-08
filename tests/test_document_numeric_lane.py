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


if __name__ == "__main__":
    unittest.main()
