"""2026-09-25b: a fetched page is not the subject's own by its title or head.

ws-7d 2186e5ad: "Synergy Research Group measured the wider cloud market ..."
was admitted as an Alphabet Claim because the third-party page it came from
is titled "Google Cloud Statistics: Market Share & Competition Report".  The
admission side now reads a public-web page the way the reinstatement side has
since 390e0d3d: the page is the subject's own by issuer, filing cover,
density or its own earnings call / results release -- never by its title or
head -- and otherwise the span must name the subject or its executive.
"""

from __future__ import annotations

import unittest
from unittest.mock import patch

from dalton_core.claim_subject import (
    event_cover_names_subject,
    own_document_evidence,
    web_span_names_subject_strictly,
)
from tests.test_claim_admission_quality import AMZN, GOOGL, NEWS_PAGE, STATS_THIRD_PARTY, _service


class WebSubjectTests(unittest.TestCase):
    """Item 2: a fetched page is not the subject's own by its title or head."""

    def test_a_third_party_page_titled_with_the_company_is_not_its_own(self) -> None:
        span = STATS_THIRD_PARTY.splitlines()[-1]
        # What admission used to accept: the head names Google.
        self.assertEqual(own_document_evidence(text=STATS_THIRD_PARTY, needles=GOOGL), "head")
        self.assertIsNone(own_document_evidence(
            text=STATS_THIRD_PARTY, needles=GOOGL, peer_needles=AMZN + ["azure"],
            include_head=False))
        self.assertFalse(web_span_names_subject_strictly(
            span=span, text=STATS_THIRD_PARTY, needles=GOOGL, peer_needles=AMZN))

    def test_the_right_ones_still_pass(self) -> None:
        # The span names the subject, or its executive.
        self.assertTrue(web_span_names_subject_strictly(
            span="Amazon last week said it would spend more.", text=NEWS_PAGE, needles=AMZN))
        self.assertTrue(web_span_names_subject_strictly(
            span="“Today, demand still significantly outstrips supply,” Garman told Bloomberg.",
            text=NEWS_PAGE, needles=AMZN))
        # The company's own earnings call or results release, fetched from the web.
        call = ("Cognizant (CTSH) Q2 2026 Earnings Call Transcript | The Motley Fool\n\n"
                "We delivered revenue growth above the top of our guidance.")
        self.assertTrue(event_cover_names_subject(call, ["cognizant", "ctsh"]))
        self.assertTrue(web_span_names_subject_strictly(
            span="We delivered revenue growth.", text=call, needles=["cognizant", "ctsh"]))
        release = ("DXC Technology Reports Fourth Quarter and Full Fiscal Year 2026 Results\n\n"
                   "Revenue declined in the quarter.")
        self.assertTrue(event_cover_names_subject(release, ["dxc", "dxc technology"]))
        # An 8-K fetched from the web is the issuer's own by its cover.
        eight_k = ("8-K\n\nUNITED STATES SECURITIES AND EXCHANGE COMMISSION\nFORM 8-K\nCURRENT "
                   "REPORT Pursuant to Section 13 OR 15(d)\nAMAZON.COM, INC.\nThe notes were sold.")
        self.assertEqual(own_document_evidence(text=eight_k, needles=AMZN, include_head=False),
                         "filing_cover")
        # A listicle or comparison has no event cover.
        self.assertFalse(event_cover_names_subject(
            "7 best Cognizant alternatives worth trying in 2026\n\nAccenture is the benchmark.",
            ["cognizant", "ctsh"]))
        self.assertFalse(event_cover_names_subject(
            "Facebook Competitors: How META Stacks Up\n\nYouTube leads in long video.",
            ["meta", "facebook"]))


class AdmissionSubjectPathTests(unittest.TestCase):
    WEB = "source:web-search"
    PLAN = {"companies": {"company:ticker:amzn": {"names": ["Amazon.com", "AMZN"]},
                          "company:ticker:googl": {"names": ["Alphabet", "Google", "GOOGL"]}}}

    def test_the_web_subject_check_no_longer_trusts_the_title(self) -> None:
        span = STATS_THIRD_PARTY.splitlines()[-1]
        service, context, connection = _service(source_ref=self.WEB,
                                                document_text=STATS_THIRD_PARTY)
        self.addCleanup(connection.close)
        with patch("dalton_core.claim_subject.writer_feed_plans", return_value=[self.PLAN]):
            check = service.admission_subject_check(context, None)
        self.assertIn("held for human review", check("company:ticker:googl", span))
        self.assertIsNone(check("company:ticker:googl", "Google Cloud revenue hit a record."))
        # The same span in a sales note keeps the old reading: its head names Google.
        service, context, connection = _service(source_ref="source:sales-notes",
                                                document_text=STATS_THIRD_PARTY)
        self.addCleanup(connection.close)
        with patch("dalton_core.claim_subject.writer_feed_plans", return_value=[self.PLAN]):
            self.assertIsNone(service.admission_subject_check(context, None)(
                "company:ticker:googl", span))

    def test_a_news_story_quoting_the_executive_is_admitted(self) -> None:
        service, context, connection = _service(source_ref=self.WEB, document_text=NEWS_PAGE)
        self.addCleanup(connection.close)
        with patch("dalton_core.claim_subject.writer_feed_plans", return_value=[self.PLAN]):
            check = service.admission_subject_check(context, None)
        self.assertIsNone(check(
            "company:ticker:amzn",
            "“As we’re investing, we’re getting five-year commitments from customers,” Garman "
            "told Bloomberg Television."))

if __name__ == "__main__":
    unittest.main()
