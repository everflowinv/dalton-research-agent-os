"""P13c: a figure belongs to a company, and that has to be checked.

The discovery search is free text, so "EPAM Systems EPAM earnings call
transcript" returned a Haier European-business call and an EOS call. Both were
read; the figures pass recorded "EPAM revenue = 14.3 billion RMB". Every digit
was verified against the bytes it cited.

Filtering the search by company is the wrong repair: an industry report worth
reading often carries no company tag, and a note comparing five vendors belongs
to none of them. The check belongs where the number is taken.
"""

from __future__ import annotations

import unittest

from dalton_core.document_figure_grade import attribution_for, figure_recordable
from dalton_core.document_subject import (
    document_names_subject,
    earnings_call_names_issuer,
    subject_label,
    subject_names,
)

HAIER = ("We want to finish the reform of the European business this year. "
         "The revenue in Europe is 14.3 billion RMB.")
EOS = ("Overall, the revenue of AUD 169 million, the highest first half "
       "revenue that EOS has ever achieved.")
EPAM_CALL = "EPAM Systems reported revenue of $1.2 billion for the quarter."
INDUSTRY = ("IT services demand is stabilising. Accenture and EPAM both cited "
            "slower discretionary spend, while Cognizant guided flat.")


class NameTests(unittest.TestCase):
    def test_a_company_is_known_by_its_name_and_its_ticker(self):
        self.assertIn("Accenture", subject_names("ACN"))
        self.assertIn("ACN", subject_names("ACN"))
        self.assertIn("International Business Machines", subject_names("IBM"))

    def test_the_model_is_told_a_name_not_a_cik(self):
        self.assertIn("Accenture", subject_label("ACN"))
        self.assertNotIn("sec-cik", subject_label("ACN"))
        self.assertEqual(subject_label(None), "the company under coverage")

    def test_an_unknown_ticker_yields_nothing_rather_than_a_guess(self):
        self.assertEqual(subject_names(None), ())
        self.assertEqual(subject_names(""), ())


class DocumentTests(unittest.TestCase):
    def test_earnings_title_requires_the_target_in_the_issuer_position(self):
        self.assertFalse(earnings_call_names_issuer(
            "Remitly Q2 2026 Earnings Call — Cognizant comparison", "CTSH"
        )["names_issuer"])
        self.assertFalse(earnings_call_names_issuer(
            "Cognizant and EPAM Q2 2026 Earnings Call discussion", "CTSH"
        )["names_issuer"])
        self.assertTrue(earnings_call_names_issuer(
            "Q2 2026 Cognizant Earnings Conference Call — EPAM comparison", "CTSH"
        )["names_issuer"])
        self.assertTrue(earnings_call_names_issuer(
            "Cognizant FY2026Q2 Earnings Call", "CTSH"
        )["names_issuer"])

    def test_the_transcripts_that_caused_this_are_refused(self):
        self.assertFalse(document_names_subject(HAIER, "EPAM")["names_subject"])
        self.assertFalse(document_names_subject(EOS, "EPAM")["names_subject"])

    def test_a_genuine_call_is_attributed(self):
        found = document_names_subject(EPAM_CALL, "EPAM")
        self.assertTrue(found["names_subject"])
        self.assertIn("EPAM Systems", found["matched"])

    def test_an_industry_report_with_no_company_tag_still_counts(self):
        # This is why the check is not a search filter: the report carries no
        # company label and is exactly the kind of document worth reading.
        for ticker in ("ACN", "EPAM", "CTSH"):
            self.assertTrue(document_names_subject(INDUSTRY, ticker)["names_subject"], ticker)
        self.assertFalse(document_names_subject(INDUSTRY, "IBM")["names_subject"])

    def test_a_ticker_inside_another_word_is_not_a_mention(self):
        self.assertFalse(document_names_subject("ACNE treatment revenue", "ACN")["names_subject"])
        self.assertFalse(document_names_subject("EPAMINONDAS said", "EPAM")["names_subject"])

    def test_case_and_punctuation_do_not_defeat_it(self):
        for text in ("accenture reported", "ACCENTURE, PLC reported", "Accenture's revenue"):
            self.assertTrue(document_names_subject(text, "ACN")["names_subject"], text)

    def test_an_unreadable_or_unknown_subject_is_reported_as_unchecked(self):
        # Refusing here would block every company nobody has named, which is a
        # configuration gap rather than evidence about the document.
        out = document_names_subject(EPAM_CALL, None)
        self.assertFalse(out["checked"])
        self.assertFalse(out["names_subject"])


class RecordableTests(unittest.TestCase):
    def test_a_filing_is_attributed_by_its_accession_without_naming_anything(self):
        self.assertEqual(attribution_for("annual-report-10k"), "sec-accession")
        self.assertTrue(figure_recordable("annual-report-10k"))

    def test_a_transcript_must_name_the_company(self):
        self.assertIsNone(attribution_for("earnings-call-transcripts"))
        self.assertFalse(figure_recordable("earnings-call-transcripts"))
        self.assertTrue(figure_recordable("earnings-call-transcripts",
                                          document_names_subject=True))

    def test_naming_the_company_does_not_make_a_news_page_a_figure_source(self):
        # The grade gate still applies: sell-side notes quote estimates.
        self.assertFalse(figure_recordable("sell-side-reports", document_names_subject=True))
        self.assertFalse(figure_recordable("competitive-landscape", document_names_subject=True))


if __name__ == "__main__":
    unittest.main()


class IndustrySubjectTests(unittest.TestCase):
    """P13f: the industry is a subject a figure can belong to."""

    IT = "industry:us-it-services"
    MARKET = ("Global IT services spending is forecast to grow 4.2% in 2026, "
              "with consulting demand stabilising.")
    APPLIANCES = ("The European white goods market saw price pressure across "
                  "refrigeration and HVAC.")

    def test_the_industry_is_named_the_way_a_company_is(self):
        self.assertIn("IT services", subject_names(self.IT))
        self.assertIn("IT services", subject_label(self.IT))

    def test_a_market_report_about_this_industry_is_attributed(self):
        self.assertTrue(document_names_subject(self.MARKET, self.IT)["names_subject"])

    def test_a_market_report_about_another_industry_is_not(self):
        # The same failure as the Haier call, one level up: a real market
        # report about the wrong market.
        self.assertFalse(document_names_subject(self.APPLIANCES, self.IT)["names_subject"])

    def test_an_industry_nobody_has_named_is_unchecked_rather_than_refused(self):
        out = document_names_subject(self.MARKET, "industry:not-configured")
        self.assertFalse(out["checked"])

    def test_a_company_document_is_still_checked_against_the_company(self):
        self.assertTrue(document_names_subject(EPAM_CALL, "EPAM")["names_subject"])
        self.assertFalse(document_names_subject(EPAM_CALL, self.IT)["names_subject"])
