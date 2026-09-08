"""P11e: a figure is only admissible if its own citation contains it."""

from __future__ import annotations

import unittest

from dalton_core.document_numeric_claim import (
    NumericCandidateError,
    numbers_in,
    validate_numeric_candidate,
    verify_numeric_candidate,
    verify_numeric_candidates,
)

QUOTE_ID = "quote:100:400:abcdef0123456789"


def candidate(**overrides):
    base = {
        "quote_id": QUOTE_ID,
        "metric_ref": "metric:new-bookings",
        "subject_as_named": "Accenture",
        "as_reported_label": "new bookings",
        "value": "21.3",
        "unit": "currency",
        "currency": "USD",
        "period": "FY2026Q3",
        "basis": "management-reported",
        "scale": "billion",
    }
    base.update(overrides)
    return base


class NumericCandidateShapeTests(unittest.TestCase):
    def test_the_shape_is_closed(self) -> None:
        with self.assertRaises(NumericCandidateError):
            validate_numeric_candidate({**candidate(), "extra": 1})
        for missing in ("metric_ref", "as_reported_label", "value", "unit",
                        "period", "basis", "scale"):
            body = candidate()
            body.pop(missing)
            with self.assertRaises(NumericCandidateError):
                validate_numeric_candidate(body)

    def test_a_currency_figure_must_name_its_currency_and_others_must_not(self) -> None:
        with self.assertRaises(NumericCandidateError):
            validate_numeric_candidate(candidate(currency=None))
        with self.assertRaises(NumericCandidateError):
            validate_numeric_candidate(candidate(unit="percent", currency="USD"))
        # A figure whose unit nobody agreed on cannot be compared or modelled.
        with self.assertRaises(NumericCandidateError):
            validate_numeric_candidate(candidate(unit="widgets", currency=None))

    def test_the_value_is_kept_as_written_not_as_a_float(self) -> None:
        # 21.30 and 21.3 are the same number but not the same reported figure;
        # binary floats would also make an exact digit check meaningless.
        self.assertEqual(validate_numeric_candidate(candidate(value="21.30"))["value"], "21.30")
        with self.assertRaises(NumericCandidateError):
            validate_numeric_candidate(candidate(value="about twenty"))
        with self.assertRaises(NumericCandidateError):
            validate_numeric_candidate(candidate(value="NaN"))


class NumericCandidateVerificationTests(unittest.TestCase):
    def test_a_figure_present_in_its_citation_is_verified(self) -> None:
        quotes = {QUOTE_ID: "New bookings were $21.3 billion for the third quarter."}
        out = verify_numeric_candidate(candidate(), quotes)
        self.assertEqual(out["value"], "21.3")
        self.assertEqual(out["claim_kind"], "quantitative")
        self.assertEqual(out["citation_text"], quotes[QUOTE_ID])
        self.assertTrue(out["citation_hash"])

    def test_a_figure_absent_from_its_citation_is_refused(self) -> None:
        # The whole point: the model may say where a number is, never what it is.
        quotes = {QUOTE_ID: "New bookings grew year over year in local currency."}
        with self.assertRaises(NumericCandidateError):
            verify_numeric_candidate(candidate(), quotes)

    def test_a_number_from_a_different_sentence_does_not_count(self) -> None:
        quotes = {QUOTE_ID: "Revenue was $17.7 billion in the third quarter."}
        with self.assertRaises(NumericCandidateError):
            verify_numeric_candidate(candidate(), quotes)

    def test_a_label_the_document_never_used_is_refused(self) -> None:
        # The slot is what makes a series; the label is what lets a reader
        # check the mapping instead of trusting it. Filers write "Net
        # revenues", "Total revenue" and "Revenues" for the same line, so the
        # label has to be this document's own wording.
        quotes = {QUOTE_ID: "Net revenues were $17.7 billion for the quarter."}
        self.assertTrue(verify_numeric_candidate(
            candidate(metric_ref="metric:revenue", as_reported_label="Net revenues",
                      value="17.7"),
            quotes,
        ))
        with self.assertRaises(NumericCandidateError):
            verify_numeric_candidate(
                candidate(metric_ref="metric:revenue",
                          as_reported_label="Total revenue", value="17.7"),
                quotes,
            )

    def test_a_near_miss_digit_is_refused(self) -> None:
        """The realistic failure: not an invented figure, a slightly wrong one.

        A model that reads "$21.3 billion" and writes 21.4 produces something
        that survives every review a human does by eye. Checking the digits
        against the cited bytes is the only place that is caught.
        """

        quotes = {QUOTE_ID: "New bookings were $21.3 billion for the third quarter."}
        with self.assertRaises(NumericCandidateError):
            verify_numeric_candidate(candidate(value="21.4"), quotes)
        # And a transposition.
        with self.assertRaises(NumericCandidateError):
            verify_numeric_candidate(candidate(value="12.3"), quotes)

    def test_citing_a_quote_that_was_never_supplied_is_refused(self) -> None:
        with self.assertRaises(NumericCandidateError):
            verify_numeric_candidate(candidate(), {"quote:other": "$21.3 billion"})

    def test_the_written_form_and_the_meant_form_both_count(self) -> None:
        # A document writes "$21.3 billion"; a model may report either that or
        # 21300000000. Refusing the written form would reject exactly the
        # figures a human can check most easily.
        written = {QUOTE_ID: "New bookings were $21.3 billion."}
        self.assertTrue(verify_numeric_candidate(candidate(), written))
        expanded = {QUOTE_ID: "New bookings were 21300000000 dollars."}
        self.assertTrue(verify_numeric_candidate(candidate(), expanded))

    def test_typography_around_numbers_does_not_defeat_the_check(self) -> None:
        for text in (
            "Revenue of $1,234.5 million",
            "Revenue of $1 234.5 million",
            "Revenue of $1 234.5 million",
        ):
            out = verify_numeric_candidate(
                candidate(metric_ref="metric:revenue", as_reported_label="Revenue",
                          value="1234.5", scale="million"),
                {QUOTE_ID: text},
            )
            self.assertEqual(out["value"], "1234.5")

    def test_a_negative_figure_matches_a_typographic_minus(self) -> None:
        out = verify_numeric_candidate(
            candidate(metric_ref="metric:operating-margin",
                      as_reported_label="Operating margin", value="-1.5",
                      unit="percent", currency=None, scale=None),
            {QUOTE_ID: "Operating margin changed by −1.5 percent."},
        )
        self.assertEqual(out["value"], "-1.5")

    def test_one_invented_figure_does_not_discard_the_good_ones(self) -> None:
        quotes = {QUOTE_ID: "Revenue was $17.7 billion and new bookings were $21.3 billion."}
        verified, refused = verify_numeric_candidates(
            [
                candidate(metric_ref="metric:revenue", as_reported_label="Revenue",
                          value="17.7"),
                candidate(value="21.3"),
                candidate(metric_ref="metric:headcount", as_reported_label="headcount",
                          value="799000", unit="count", currency=None, scale=None),
            ],
            quotes,
        )
        self.assertEqual(
            [item["metric_ref"] for item in verified],
            ["metric:revenue", "metric:new-bookings"],
        )
        self.assertEqual(len(refused), 1)
        self.assertEqual(refused[0]["metric_ref"], "metric:headcount")
        # The refusal says why, so a missing figure is visible rather than silent.
        self.assertIn("citation does not contain", refused[0]["reason"])


class NumberScanningTests(unittest.TestCase):
    def test_numbers_are_read_the_way_a_reader_sees_them(self) -> None:
        self.assertEqual(numbers_in("no digits here"), set())
        found = numbers_in("Revenue $1,234.5m, up 7.2%, down −0.3 points")
        self.assertIn(__import__("decimal").Decimal("1234.5"), found)
        self.assertIn(__import__("decimal").Decimal("7.2"), found)
        self.assertIn(__import__("decimal").Decimal("-0.3"), found)


if __name__ == "__main__":
    unittest.main()
