"""P11r: the discovery pass inside the lane, over the hermetic harness.

The pieces were already tested apart -- a window is read for names, a name is
verified against its quote, two documents make a requirement.  What this covers
is the join: that running the pass over a real bound window journals what it
found, that reading the same window twice adds nothing, and that a requirement
learned this way is what the figures pass is then asked for.
"""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from dalton_core.document_extraction_cli import discovery_worthy
from dalton_core.metric_discovery_extraction import TASK_REF, build_work
from tests.test_document_extraction import ExtractionHarness, OWNER


def response(*metrics) -> dict:
    return {"schema_version": "0.1", "metrics": list(metrics)}


def metric(quote_id, **overrides):
    base = {
        "quote_id": quote_id,
        "metric_ref": "metric:client-decisions",
        "label": "client decisions",
        "unit": "count",
        "evidence_phrase": "client decisions",
    }
    base.update(overrides)
    return base


class MetricDiscoveryLaneTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.h = ExtractionHarness(Path(self.temp.name))
        self.addCleanup(self.h.close)

    def _learn(self, output):
        self.h.enable_fixture(output)
        return self.h.service.generate_metric_discovery(
            **self.h.params, expected_context_hash=self.h.context()["content_hash"],
        )

    def quote_id(self):
        return self.h.context()["quotes"][0]["quote_id"]

    def test_a_window_the_model_cannot_be_asked_about_is_gated_not_guessed(self):
        result = self.h.service.generate_metric_discovery(
            **self.h.params, expected_context_hash=self.h.context()["content_hash"],
        )
        self.assertEqual(result["status"], "gated")
        self.assertEqual(self.h.missions.metric_observations(self.h.review["company_ref"]), [])

    def test_a_named_measure_is_journalled_but_is_not_yet_a_requirement(self):
        company = self.h.review["company_ref"]
        result = self._learn(response(metric(self.quote_id())))
        self.assertEqual(result["status"], "read")
        self.assertEqual(result["recorded"], ["metric:client-decisions"])
        self.assertEqual(len(self.h.missions.metric_observations(company)), 1)
        # One transcript said so. That is an observation, not a requirement.
        self.assertEqual(self.h.missions.metric_requirements(company), [])

    def test_reading_the_same_window_again_replays_and_adds_nothing(self):
        company = self.h.review["company_ref"]
        first = self._learn(response(metric(self.quote_id())))
        calls = self.h.adapter.calls
        again = self.h.service.generate_metric_discovery(
            **self.h.params, expected_context_hash=self.h.context()["content_hash"],
        )
        # The saved result is recovered rather than paid for again, and the
        # journal cannot grow from it.
        self.assertEqual(self.h.adapter.calls, calls)
        self.assertEqual(again["proposals"], first["proposals"])
        self.assertFalse(first["replayed"])
        self.assertTrue(again["replayed"])
        self.assertEqual(again["recorded"], [])
        self.assertEqual(len(self.h.missions.metric_observations(company)), 1)

    def test_wording_the_window_never_used_is_refused_and_not_journalled(self):
        company = self.h.review["company_ref"]
        result = self._learn(response(metric(
            self.quote_id(), metric_ref="metric:same-store-sales",
            label="same-store sales", unit="currency", evidence_phrase="same-store sales",
        )))
        self.assertEqual(result["proposals"], [])
        self.assertEqual(len(result["refused"]), 1)
        self.assertEqual(self.h.missions.metric_observations(company), [])

    def test_a_learned_requirement_is_what_the_figures_pass_then_asks_for(self):
        company = self.h.review["company_ref"]
        self._learn(response(metric(self.quote_id())))
        # A second, distinct document naming the same measure. Journalled
        # directly: what is under test is that a corroborated requirement
        # reaches the figures pass, not the model call that produced it.
        self.h.missions.record_metric_observations(
            company_ref=company, observed_by=OWNER,
            proposals=[{**metric(self.quote_id()), "document_ref": "alphaengine-doc:other",
                        "citation_text": "client decisions"}],
        )
        slots = self.h.service.numeric_slots(self.h.context())
        self.assertIn("metric:client-decisions", [slot["metric_ref"] for slot in slots])
        # The universal floor is still asked for beside it.
        self.assertIn("metric:revenue", [slot["metric_ref"] for slot in slots])

    def test_the_discovery_order_is_its_own_call_and_rebuilds_as_itself(self):
        from dalton_core.document_extraction import (
            DocumentExtractionModelWorker, build_work as qualitative,
        )

        context = self.h.context()
        work = build_work(context)
        self.assertEqual(work.metadata["task_ref"], TASK_REF)
        self.assertNotEqual(work.id, qualitative(context).id)
        # Rebuilding it with the qualitative builder would call every discovery
        # call drift, and the pass would never run at all.
        self.assertEqual(
            DocumentExtractionModelWorker._rebuild(work, context).to_dict(), work.to_dict()
        )

    def test_the_lane_asks_only_the_documents_that_react_to_results(self):
        # A filing reports; a note and a call react. The figures pass wants the
        # first, this one wants the second, so they never contend for a window.
        self.assertTrue(discovery_worthy("earnings-call-transcripts"))
        self.assertTrue(discovery_worthy("sell-side-reports"))
        self.assertFalse(discovery_worthy("annual-report-10k"))
        self.assertFalse(discovery_worthy(None))


if __name__ == "__main__":
    unittest.main()
