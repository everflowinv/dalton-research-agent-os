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


if __name__ == "__main__":
    unittest.main()
