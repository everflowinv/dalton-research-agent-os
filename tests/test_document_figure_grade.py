"""P11v: a figure is graded by the document it came from, not by a model."""

from __future__ import annotations

import unittest

from dalton_core.document_figure_grade import (
    FILED,
    GRADE_BY_SPEC,
    SPOKEN,
    FigureGradeError,
    basis_for,
    figure_worthy,
    grade_for,
    qualify,
    require_grade,
)


class GradeTests(unittest.TestCase):
    def test_a_filed_document_and_a_call_are_not_the_same_thing(self):
        self.assertEqual(grade_for("annual-report-10k"), FILED)
        self.assertEqual(grade_for("quarterly-report-10q"), FILED)
        self.assertEqual(grade_for("company-press-release"), FILED)
        self.assertEqual(grade_for("earnings-call-transcripts"), SPOKEN)
        self.assertNotEqual(FILED, SPOKEN)

    def test_sell_side_research_is_not_a_place_to_take_a_figure_from(self):
        # A note quoting "we model revenue of $17.9bn" passes a digit check
        # perfectly and is the analyst's estimate, not the company's result.
        self.assertIsNone(grade_for("sell-side-reports"))
        self.assertFalse(figure_worthy("sell-side-reports"))

    def test_a_news_page_is_not_either(self):
        for spec in ("competitive-landscape", "industry-demand", "management-changes"):
            self.assertFalse(figure_worthy(spec), spec)

    def test_an_unknown_or_absent_kind_is_refused_rather_than_defaulted(self):
        # Defaulting an unrecognised document kind to a grade is how a figure
        # from somewhere nobody vetted acquires a provenance it never earned.
        for spec in (None, "", "invented-spec", 7, object()):
            self.assertIsNone(grade_for(spec))
            self.assertFalse(figure_worthy(spec))
            with self.assertRaises(FigureGradeError):
                require_grade(spec)

    def test_every_graded_kind_has_a_basis_and_a_qualifier(self):
        for spec, grade in GRADE_BY_SPEC.items():
            self.assertTrue(basis_for(grade), spec)
            self.assertIn(grade, (FILED, SPOKEN))


class StatementTests(unittest.TestCase):
    def test_a_spoken_figure_says_so_in_its_own_statement(self):
        said = qualify("ACCENTURE reported Net revenues of USD 17.7 billion", SPOKEN)
        self.assertIn("spoken on the earnings call", said)
        self.assertIn("not read from a filed statement", said)
        self.assertTrue(said.endswith("."))

    def test_a_filed_figure_says_that_instead(self):
        filed = qualify("ACCENTURE reported Net revenues of USD 17.7 billion", FILED)
        self.assertIn("published by the company", filed)
        self.assertNotIn("spoken", filed)

    def test_the_two_statements_differ_so_the_claim_hashes_differ(self):
        # Otherwise the same number filed and spoken would be one record and
        # the distinction would exist only in a table nobody joins.
        statement = "ACCENTURE reported Net revenues of USD 17.7 billion"
        self.assertNotEqual(qualify(statement, FILED), qualify(statement, SPOKEN))

    def test_a_trailing_stop_is_not_doubled(self):
        self.assertNotIn("..", qualify("Revenue was 17.7 billion.", FILED))

    def test_an_empty_statement_or_unknown_grade_is_refused(self):
        with self.assertRaises(FigureGradeError):
            qualify("   ", FILED)
        with self.assertRaises(FigureGradeError):
            qualify("Revenue was 17.7 billion", "invented-grade")
        with self.assertRaises(FigureGradeError):
            basis_for("invented-grade")


class AttributionTests(unittest.TestCase):
    """P12h: a number is worthless if it is filed under the wrong company.

    Live, "EPAM Systems EPAM earnings call transcript" returned a Haier
    European-business call (RMB, refrigerators) and an EOS call (AUD). Both
    were acquired under EPAM, both were read, and the figures pass recorded
    "EPAM revenue = 14.3 billion RMB". Every digit was verified against the
    bytes it cited; the bytes were about another company.
    """

    def test_a_sec_filing_is_attributed_by_its_own_accession(self):
        from dalton_core.document_figure_grade import attribution_for, figure_recordable

        self.assertEqual(attribution_for("annual-report-10k"), "sec-accession")
        self.assertTrue(figure_recordable("annual-report-10k"))
        self.assertTrue(figure_recordable("quarterly-report-10q"))

    def test_a_free_text_search_result_is_attributed_by_nothing_on_its_own(self):
        from dalton_core.document_figure_grade import attribution_for, figure_recordable

        self.assertIsNone(attribution_for("earnings-call-transcripts"))
        self.assertFalse(figure_recordable("earnings-call-transcripts"))
        # ...but it earns attribution by naming the company, which is what
        # separates a real EPAM call from the Haier one that was filed as one.
        self.assertTrue(figure_recordable("earnings-call-transcripts",
                                          document_names_subject=True))

    def test_reading_is_still_allowed_where_recording_is_not(self):
        # The refusal is about storing a number against a company, not about
        # reading the document -- the prose and discovery passes still use it.
        from dalton_core.document_figure_grade import figure_recordable, figure_worthy

        self.assertTrue(figure_worthy("earnings-call-transcripts"))
        self.assertFalse(figure_recordable("earnings-call-transcripts"))

    def test_nothing_unrecognised_is_recordable(self):
        from dalton_core.document_figure_grade import figure_recordable

        for spec in (None, "", "sell-side-reports", "invented", 7):
            self.assertFalse(figure_recordable(spec), spec)

    def test_the_lane_reads_transcripts_and_decides_attribution_per_document(self):
        # Filtering them out of the lane was the blunt fix. A transcript that
        # names the company is a good figure source; one that does not is
        # another company's call, and only the document's text can tell them
        # apart -- so the lane reads both and the pass refuses one.
        from dalton_core.document_extraction_cli import numeric_worthy

        self.assertTrue(numeric_worthy("annual-report-10k"))
        self.assertTrue(numeric_worthy("earnings-call-transcripts"))
        self.assertFalse(numeric_worthy("sell-side-reports"))
        self.assertFalse(numeric_worthy("competitive-landscape"))


if __name__ == "__main__":
    unittest.main()
