"""P11p: reading a window for what the market judges a company on."""

from __future__ import annotations

import json
import unittest

from dalton_core.metric_discovery_extraction import (
    MAX_METRICS_PER_WINDOW,
    MetricDiscoveryExtractionError,
    build_prompt,
    build_request,
    build_work,
    parse_response,
    proposals_from_window,
    worthy_spec,
)

QUOTE = "quote:0:400:bbbbbbbbbbbbbbbb"
CONTEXT = {
    "company_ref": "company:sec-cik:0001467373",
    "document_ref": "alphaengine-doc:sell-side-1",
    "content_hash": "0" * 64,
    "created_at": "2026-09-08T00:00:00.000000+00:00",
    "id": "extraction-context:9",
    "source_manifest_ref": "manifest:9",
    "quotes": [{
        "quote_id": QUOTE,
        "raw_text": "We remain focused on new bookings, which decelerated again this quarter.",
    }],
}


def metric(**overrides):
    base = {
        "quote_id": QUOTE,
        "metric_ref": "metric:new-bookings",
        "label": "new bookings",
        "unit": "currency",
        "evidence_phrase": "new bookings",
    }
    base.update(overrides)
    return base


def response(*metrics) -> str:
    return json.dumps({"schema_version": "0.1", "metrics": list(metrics)})


class SourceChoiceTests(unittest.TestCase):
    def test_only_documents_that_say_what_is_watched_are_asked(self) -> None:
        # A 10-K says what a company must report; a note says what it is
        # judged on, and only the second question is being asked.
        self.assertTrue(worthy_spec("sell-side-reports"))
        self.assertTrue(worthy_spec("earnings-call-transcripts"))
        self.assertFalse(worthy_spec("annual-report-10k"))
        self.assertFalse(worthy_spec(None))


class PromptTests(unittest.TestCase):
    def test_it_asks_for_names_and_refuses_values(self) -> None:
        prompt = build_prompt(build_request(CONTEXT))
        self.assertIn("Do NOT report any values", prompt)
        # A measure discussed without a figure still counts; a number in a
        # table nobody comments on does not.
        self.assertIn("even when it quotes no figure", prompt)
        self.assertIn("table nobody comments on", prompt)
        self.assertIn("empty metrics list", prompt)
        self.assertIn("UNTRUSTED_SOURCE_DATA", prompt)
        self.assertIn("source categories are distinct", prompt)
        self.assertIn("do not assign it", prompt)
        self.assertNotIn("You are reading one window of sell-side", prompt)

    def test_prompt_contract_change_has_a_new_work_identity(self) -> None:
        work = build_work(CONTEXT)
        self.assertEqual(work.metadata["task_ref"], "task:metric-discovery-extraction:0.2")
        self.assertIn("source categories are distinct", work.question)


class ResponseTests(unittest.TestCase):
    def test_a_supported_metric_becomes_a_proposal_carrying_its_document(self) -> None:
        out = proposals_from_window(build_request(CONTEXT), response(metric()))
        self.assertEqual(len(out["proposals"]), 1)
        proposal = out["proposals"][0]
        self.assertEqual(proposal["metric_ref"], "metric:new-bookings")
        # Corroboration is counted in documents, so a proposal that forgot
        # where it came from could not be told from the same window twice.
        self.assertEqual(proposal["document_ref"], CONTEXT["document_ref"])

    def test_wording_the_window_never_used_is_refused(self) -> None:
        out = proposals_from_window(
            build_request(CONTEXT),
            response(metric(metric_ref="metric:utilization", label="utilisation",
                            evidence_phrase="utilisation rate")),
        )
        self.assertEqual(out["proposals"], [])
        self.assertEqual(len(out["refused"]), 1)

    def test_a_good_proposal_survives_an_unsupported_one(self) -> None:
        out = proposals_from_window(
            build_request(CONTEXT),
            response(metric(), metric(metric_ref="metric:headcount", label="headcount",
                                      evidence_phrase="headcount")),
        )
        self.assertEqual([item["metric_ref"] for item in out["proposals"]],
                         ["metric:new-bookings"])
        self.assertEqual(len(out["refused"]), 1)

    def test_an_empty_answer_is_valid(self) -> None:
        out = proposals_from_window(build_request(CONTEXT), response())
        self.assertEqual((out["proposals"], out["refused"]), ([], []))

    def test_a_malformed_answer_is_refused(self) -> None:
        for bad in ("nope", json.dumps({"metrics": []}),
                    json.dumps({"schema_version": "0.2", "metrics": []}),
                    json.dumps({"schema_version": "0.1", "metrics": {}})):
            with self.assertRaises(MetricDiscoveryExtractionError):
                parse_response(bad)

    def test_a_window_cannot_name_an_unbounded_number_of_measures(self) -> None:
        with self.assertRaises(MetricDiscoveryExtractionError):
            parse_response(response(*[metric() for _ in range(MAX_METRICS_PER_WINDOW + 1)]))


class WorkOrderTests(unittest.TestCase):
    def test_metric_discovery_has_an_independent_configured_budget(self):
        configured = build_work(CONTEXT, model_config={"purpose_call_budgets": {
            "metric_discovery_extraction": {
                "max_input_tokens": 17000, "max_output_tokens": 901,
                "max_cost_usd": 0.02, "timeout_seconds": 41,
            },
            "document_numeric_extraction": {"max_output_tokens": 222},
        }})
        self.assertEqual(configured.budget, {
            "max_input_tokens": 17000, "max_output_tokens": 901,
            "max_total_tokens": 17901, "max_cost_usd": 0.02, "max_seconds": 41,
        })
        self.assertNotEqual(configured.id, build_work(CONTEXT).id)

    def test_it_is_its_own_call(self) -> None:
        work = build_work(CONTEXT)
        self.assertTrue(work.id.startswith("work:metric-discovery-"))
        self.assertEqual(work.id, build_work(CONTEXT).id)
        self.assertTrue(work.metadata["candidate_only"])

    def test_a_moved_window_is_a_different_call(self) -> None:
        moved = {**CONTEXT, "content_hash": "1" * 64}
        self.assertNotEqual(build_work(CONTEXT).id, build_work(moved).id)


if __name__ == "__main__":
    unittest.main()
