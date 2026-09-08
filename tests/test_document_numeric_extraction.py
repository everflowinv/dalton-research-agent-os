"""P11i: a window is asked for named slots, and its answer is distrusted."""

from __future__ import annotations

import json
import unittest

from dalton_core.document_numeric_extraction import (
    MAX_FIGURES_PER_WINDOW,
    NumericExtractionError,
    build_prompt,
    build_request,
    extract_from_window,
    parse_response,
)

QUOTE = "quote:0:200:aaaaaaaaaaaaaaaa"
CONTEXT = {
    "company_ref": "company:sec-cik:0001467373",
    "document_ref": "sec:filing:0001467373-25-000217",
    "quotes": [{
        "quote_id": QUOTE,
        "raw_text": "Net revenues were $17.7 billion and new bookings were $21.3 billion.",
    }],
}
SLOTS = [
    {"metric_ref": "metric:revenue", "label": "revenue", "unit": "currency",
     "prompt": "total revenue for the period as reported"},
    {"metric_ref": "metric:new-bookings", "label": "new bookings", "unit": "currency",
     "prompt": "new bookings for the period"},
]


def figure(**overrides):
    base = {
        "quote_id": QUOTE,
        "metric_ref": "metric:revenue",
        "as_reported_label": "Net revenues",
        "value": "17.7",
        "unit": "currency",
        "currency": "USD",
        "period": "FY2026Q3",
        "basis": "gaap-reported",
        "scale": "billion",
    }
    base.update(overrides)
    return base


def response(*figures) -> str:
    return json.dumps({"schema_version": "0.1", "figures": list(figures)})


class RequestTests(unittest.TestCase):
    def test_a_request_carries_the_window_and_the_slots(self) -> None:
        request = build_request(CONTEXT, SLOTS)
        self.assertEqual(
            [slot["metric_ref"] for slot in request["slots"]],
            ["metric:revenue", "metric:new-bookings"],
        )
        self.assertEqual(request["quotes"][0]["quote_id"], QUOTE)

    def test_a_company_that_owes_nothing_is_never_asked(self) -> None:
        # Nothing owed must not become a model call.
        with self.assertRaises(NumericExtractionError):
            build_request(CONTEXT, [])

    def test_a_window_with_no_quotes_is_refused(self) -> None:
        with self.assertRaises(NumericExtractionError):
            build_request({**CONTEXT, "quotes": []}, SLOTS)

    def test_the_prompt_tells_the_model_the_checks_it_will_face(self) -> None:
        prompt = build_prompt(build_request(CONTEXT, SLOTS))
        # A model told the digits are verified returns nothing rather than
        # guessing, which is the wanted outcome for an absent figure.
        self.assertIn("checked against that quote", prompt)
        self.assertIn("empty figures list", prompt)
        self.assertIn("Do not calculate", prompt)
        # The window's own text is fenced as untrusted.
        self.assertIn("UNTRUSTED_SOURCE_DATA", prompt)


class ResponseTests(unittest.TestCase):
    def test_a_well_formed_figure_is_verified(self) -> None:
        out = extract_from_window(build_request(CONTEXT, SLOTS), response(figure()))
        self.assertEqual(len(out["verified"]), 1)
        self.assertEqual(out["verified"][0]["metric_ref"], "metric:revenue")
        self.assertEqual(out["verified"][0]["as_reported_label"], "Net revenues")
        self.assertEqual(out["refused"], [])

    def test_a_figure_whose_number_is_not_in_the_quote_is_refused(self) -> None:
        out = extract_from_window(
            build_request(CONTEXT, SLOTS), response(figure(value="18.2"))
        )
        self.assertEqual(out["verified"], [])
        self.assertEqual(len(out["refused"]), 1)

    def test_a_label_the_document_never_used_is_refused(self) -> None:
        # The mapping from slot to wording is what a reviewer checks, so the
        # wording has to be the document's own.
        out = extract_from_window(
            build_request(CONTEXT, SLOTS),
            response(figure(as_reported_label="Total revenue")),
        )
        self.assertEqual(out["verified"], [])
        self.assertIn("label", out["refused"][0]["reason"])

    def test_a_figure_for_a_slot_nobody_asked_for_is_refused(self) -> None:
        # Otherwise this is the qualitative pass wearing a number, and the
        # answer stops being countable against a requirement.
        out = extract_from_window(
            build_request(CONTEXT, [SLOTS[0]]),
            response(figure(metric_ref="metric:new-bookings",
                            as_reported_label="new bookings", value="21.3")),
        )
        self.assertEqual(out["verified"], [])
        self.assertIn("did not request", out["refused"][0]["reason"])

    def test_a_good_figure_survives_a_bad_one_beside_it(self) -> None:
        out = extract_from_window(
            build_request(CONTEXT, SLOTS),
            response(
                figure(),
                figure(metric_ref="metric:new-bookings",
                       as_reported_label="new bookings", value="99.9"),
            ),
        )
        self.assertEqual([item["metric_ref"] for item in out["verified"]], ["metric:revenue"])
        self.assertEqual(len(out["refused"]), 1)

    def test_an_empty_answer_is_valid(self) -> None:
        out = extract_from_window(build_request(CONTEXT, SLOTS), response())
        self.assertEqual((out["verified"], out["refused"]), ([], []))

    def test_a_malformed_response_is_refused_not_partially_read(self) -> None:
        for bad in ("not json", json.dumps({"figures": []}),
                    json.dumps({"schema_version": "9.9", "figures": []}),
                    json.dumps({"schema_version": "0.1", "figures": {}})):
            with self.assertRaises(NumericExtractionError):
                parse_response(bad)

    def test_a_window_cannot_return_an_unbounded_number_of_figures(self) -> None:
        too_many = response(*[figure() for _ in range(MAX_FIGURES_PER_WINDOW + 1)])
        with self.assertRaises(NumericExtractionError):
            parse_response(too_many)


class WorkOrderTests(unittest.TestCase):
    def context(self) -> dict:
        return {
            **CONTEXT,
            "content_hash": "0" * 64,
            "created_at": "2026-09-08T00:00:00.000000+00:00",
            "id": "extraction-context:1",
            "source_manifest_ref": "manifest:1",
            "offset": 0,
            "end": 200,
        }

    def test_the_same_ask_replays_as_the_same_call(self) -> None:
        from dalton_core.document_numeric_extraction import build_work

        first = build_work(self.context(), SLOTS)
        self.assertEqual(first.id, build_work(self.context(), SLOTS).id)

    def test_asking_for_different_figures_is_a_different_call(self) -> None:
        from dalton_core.document_numeric_extraction import build_work

        # Otherwise a window asked for revenue would replay its old answer
        # when later asked for free cash flow, and the second ask would
        # silently return the first ask's figures.
        one = build_work(self.context(), [SLOTS[0]])
        both = build_work(self.context(), SLOTS)
        self.assertNotEqual(one.id, both.id)

    def test_it_is_its_own_work_order_not_the_qualitative_one(self) -> None:
        from dalton_core.document_extraction import build_work as qualitative
        from dalton_core.document_numeric_extraction import build_work

        numeric = build_work(self.context(), SLOTS)
        self.assertTrue(numeric.id.startswith("work:document-numeric-"))
        self.assertNotEqual(numeric.id, qualitative(self.context()).id)
        # The answer is a short list of figures or nothing, not prose.
        self.assertLess(
            numeric.budget["max_output_tokens"],
            qualitative(self.context()).budget["max_output_tokens"],
        )
        self.assertTrue(numeric.metadata["candidate_only"])


if __name__ == "__main__":
    unittest.main()
