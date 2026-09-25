"""2026-09-25b: what the document qualitative rule could not see at admission.

The post-deploy sample of freshly admitted ws-7d Claims found twelve AMZN
Claims from amraandelma.com's "TOP 20 AMAZON ADS STATISTICS 2026", one citing
"Amazon's Q3 2026 Seller Advertising Activity Report" a week before Q3 ended,
and "declined to forecast its spending for the following year" filed with
period 2026 from a page published in August 2026.  Three deterministic holds
(``claim_admission_quality``): an SEO statistics compilation, a temporally
impossible statement, a relative year that cannot be anchored -- and the
anchored rewrite when it can.
"""

from __future__ import annotations

import sqlite3
import unittest

from dalton_core.claim_admission_quality import (
    anchor_relative_years,
    document_title,
    period_intervals,
    relative_year_mentions,
    statistics_compilation_evidence,
    temporal_impossibility,
)

AMZN = ["amazon", "amazon.com", "amzn", "aws"]
GOOGL = ["alphabet", "google", "googl"]

#: The shape of the amraandelma page: a statistics title, one figure per paragraph.
SEO_PAGE = "\n\n".join(
    ["TOP 20 AMAZON ADS STATISTICS 2026 THAT REVEAL EXPLOSIVE RETAIL MEDIA DOMINANCE",
     "info@amraandelma.com | 1-646-518-7743", "Home", "OUR WORK"]
    + [f"{n}. In 2026 Amazon's ad metric number {n} reached ${n}.{n} billion, a {n}% increase "
       "over the prior period, according to an industry outlook." for n in range(1, 13)]
    + ["In Q3 2026, Amazon’s ad revenue is projected to reach $22.4 billion, with a record "
       "68,000 new third-party sellers launching their first paid campaigns during July 2026 "
       "alone, according to Amazon’s Q3 2026 Seller Advertising Activity Report."])
NEWS_PAGE = "\n\n".join(
    ["Amazon cloud chief says potential AI business is 'just massive' - The Business Times",
     "Published Tue, Aug 4, 2026 · 10:32 AM",
     "Amazon last week said it would spend US$220 billion in capital expenditures in 2026, "
     "up from a prior forecast of US$200 billion, reflecting the price of memory.",
     "“As we’re investing, we’re getting five-year commitments from customers,” Garman told "
     "Bloomberg Television. “Today, demand still significantly outstrips supply.”",
     "Garman declined to forecast the company’s spending next year, but said the potential "
     "artificial intelligence business for the company is “just massive.”"])
STATS_THIRD_PARTY = "\n\n".join(
    ["Google Cloud Statistics: Market Share & Competition Report 2026 - TechnologyChecker.io",
     "Google Cloud revenue hit $24.8B in Q2 2026, up 82%. But only 19% of enterprises run "
     "significant workloads on Google Cloud versus 50% on AWS and 45% on Azure."]
    + [f"AWS holds {30 + n}% of the market in the {n}th slice while Azure holds {20 + n}% and "
       f"Google Cloud {10 + n}%, per third-party estimates of IaaS spending." for n in range(10)]
    + ["Synergy Research Group measured the wider market growing 43% year over year in Q2 "
       "2026, the fastest in eight years, which its chief analyst attributed to AI demand."])


class StatisticsCompilationTests(unittest.TestCase):
    def test_the_seo_statistics_page_is_recognised_and_says_why(self) -> None:
        evidence = statistics_compilation_evidence(SEO_PAGE)
        self.assertIsNotNone(evidence)
        self.assertEqual(evidence["title_marker"].lower(), "statistics")
        self.assertGreaterEqual(evidence["numeric_paragraphs"], 8)
        self.assertTrue(document_title(SEO_PAGE).startswith("TOP 20 AMAZON ADS STATISTICS"))
        self.assertIsNotNone(statistics_compilation_evidence(STATS_THIRD_PARTY))

    def test_news_listicles_and_filings_are_not(self) -> None:
        # A news story with figures has no statistics title.
        self.assertIsNone(statistics_compilation_evidence(NEWS_PAGE))
        # A listicle title without the word, however many numbers it carries.
        listicle = SEO_PAGE.replace(
            "TOP 20 AMAZON ADS STATISTICS 2026 THAT REVEAL EXPLOSIVE RETAIL MEDIA DOMINANCE",
            "Top 10 IBM Competitors and Alternatives in 2026")
        self.assertIsNone(statistics_compilation_evidence(listicle))
        # A statistics title over a page with few figures is not a compilation.
        self.assertIsNone(statistics_compilation_evidence(
            "Amazon Advertising Statistics\n\nA short essay on retail media, no numbers."))
        self.assertIsNone(statistics_compilation_evidence(None))


class TemporalImpossibilityTests(unittest.TestCase):
    AS_OF = "2026-09-24"

    def test_a_report_for_an_unfinished_calendar_quarter_cannot_exist(self) -> None:
        span = SEO_PAGE.splitlines()[-1]
        reason = temporal_impossibility(
            statement="Amazon's Q3 2026 ad revenue is projected to increase over Q3 2024.",
            period="Q3 2026", document_date=self.AS_OF, cited_span=span)
        self.assertIn("report_not_yet_possible", reason)
        self.assertIn("Q3 2026", reason)
        # A week later the quarter is over and the same text is possible.
        self.assertIsNone(temporal_impossibility(
            statement="Amazon's Q3 2026 ad revenue is projected to increase over Q3 2024.",
            period="Q3 2026", document_date="2026-10-30", cited_span=span))

    def test_an_unfinished_period_stated_as_an_outcome_is_held(self) -> None:
        for statement in (
            "Amazon's total ad revenue growth rate accelerated in 2026, with AI tools.",
            "Amazon's retail media ad spend share rose in 2026 while Walmart and Target's "
            "networks held a smaller share.",
            "Amazon's 2026 Prime Video ad revenue surpassed its earlier projection.",
        ):
            reason = temporal_impossibility(statement=statement, period="2026",
                                            document_date=self.AS_OF)
            self.assertIn("unfinished_period_outcome_as_fact", reason or "", statement)

    def test_forecasts_to_date_outcomes_fiscal_periods_and_the_future_pass(self) -> None:
        for statement, period in (
            ("Amazon's Q4 2026 holiday quarter ad revenue is forecast to rise versus Q4 2024.",
             "Q4 2026"),
            ("Google Cloud's revenue growth accelerated through 2026, after a lower 2025 rate.",
             "Q1 2026 and Q2 2026"),
            ("TriZetto grew faster than the overall company in the first half of 2026.",
             "first half of 2026"),
            ("Accenture's Q3 FY26 operating margin increased year-over-year.", "Q3 FY26"),
            ("The FTC case is scheduled for a bench trial starting February 2027.", "2027"),
            ("Meta trades at a low-teens multiple of 2027 GAAP EPS.", "2027"),
            ("Amazon raised its 2026 capital expenditure plan above its prior forecast.", "2026"),
            ("Amazon's Q1 2026 ad revenue rose, per Amazon's Q1 2026 earnings release.",
             "Q1 2026"),
        ):
            self.assertIsNone(temporal_impossibility(
                statement=statement, period=period, document_date=self.AS_OF,
                cited_span=statement), statement)
        # A fiscal "Q3 2026 results" in a span is not pinned to the calendar.
        self.assertIsNone(temporal_impossibility(
            statement="Accenture reported strong profitability.", period="Q3 FY26",
            document_date="2026-09-07",
            cited_span="Accenture Reports Q3 2026 results for fiscal 2026 (June 2026 release)."))
        self.assertIsNone(temporal_impossibility(
            statement="x rose in 2026", period="2026", document_date=None))

    def test_periods_are_read_as_calendar_intervals(self) -> None:
        tokens = {item["token"]: (item["start"].isoformat(), item["end"].isoformat())
                  for item in period_intervals(
                      "Q3 2026, 2Q26, H1 2026, July 2026, March 31, 2026, the second "
                      "half of 2025 and 2027; not FY2026 nor fiscal 2025")}
        self.assertEqual(tokens["Q3 2026"], ("2026-07-01", "2026-09-30"))
        self.assertEqual(tokens["2Q26"], ("2026-04-01", "2026-06-30"))
        self.assertEqual(tokens["H1 2026"], ("2026-01-01", "2026-06-30"))
        self.assertEqual(tokens["July 2026"], ("2026-07-01", "2026-07-31"))
        self.assertEqual(tokens["March 31, 2026"], ("2026-03-31", "2026-03-31"))
        self.assertEqual(tokens["second half of 2025"], ("2025-07-01", "2025-12-31"))
        self.assertIn("2027", tokens)
        self.assertNotIn("2025", tokens)  # "fiscal 2025"


class RelativeYearTests(unittest.TestCase):
    STATEMENT = ("Amazon declined to forecast its spending for the following year, while its "
                 "cloud chief called the potential AI business for the company 'just massive'.")

    def test_a_retrieval_date_cannot_anchor_it_so_it_is_held(self) -> None:
        # The live case: a fetched page, whose only date is when it was fetched.
        result = anchor_relative_years(statement=self.STATEMENT, period="2026",
                                       document_date="2026-09-24", date_basis="retrieved")
        self.assertIn("no published date", result["hold"])
        self.assertEqual(result["statement"], self.STATEMENT)
        self.assertEqual(result["period"], "2026")

    def test_a_published_date_writes_the_year_into_statement_and_period(self) -> None:
        result = anchor_relative_years(statement=self.STATEMENT, period="2026",
                                       document_date="2026-08-04", date_basis="published")
        self.assertIsNone(result["hold"])
        self.assertIn("the following year (2027)", result["statement"])
        self.assertEqual(result["period"], "2027")
        self.assertEqual(result["anchored"]["year"], 2027)
        again = anchor_relative_years(statement=result["statement"], period=result["period"],
                                      document_date="2026-08-04", date_basis="published")
        self.assertEqual(again["statement"], result["statement"])  # idempotent
        chinese = anchor_relative_years(statement="管理层表示明年资本开支将继续上升。",
                                        period="当前", document_date="2026-08-04",
                                        date_basis="published")
        self.assertEqual((chinese["statement"], chinese["period"]),
                         ("管理层表示明年 (2027)资本开支将继续上升。", "2027"))

    def test_what_cannot_be_determined_is_held(self) -> None:
        fiscal = anchor_relative_years(
            statement="Accenture expects a healthy increase in bookings in the next fiscal year.",
            period="FY2026", document_date="2026-09-25", date_basis="published")
        self.assertIn("fiscal", fiscal["hold"])
        other_year = anchor_relative_years(
            statement="In 2024 the vendor signed the deal and the following year it expanded.",
            period="2024", document_date="2026-09-25", date_basis="published")
        self.assertIn("also names", other_year["hold"])
        mixed = anchor_relative_years(
            statement="Sales fell last year and will recover next year.", period="2026",
            document_date="2026-09-25", date_basis="published")
        self.assertIn("different years", mixed["hold"])

    def test_comparison_bases_and_near_misses_are_not_relative_years(self) -> None:
        for statement in ("Revenue grew versus last year.", "Margins were in line with last year.",
                          "Utilization declined year-over-year.", "较去年同期下降。",
                          "可见度延伸至较远的未来年份。", "EPAM expects growth this year.",
                          "Bookings were up from the prior year."):
            self.assertEqual(relative_year_mentions(statement), [], statement)


def _service(*, source_ref, document_text, document_date="2026-09-24", basis="retrieved",
             universe=None, plan=None):
    from dalton_core.document_extraction import DocumentExtractionService

    connection = sqlite3.connect(":memory:")
    connection.row_factory = sqlite3.Row
    connection.execute("CREATE TABLE document_provenance_records(document_ref TEXT, title TEXT)")
    universe = universe or [{"company_ref": "company:ticker:amzn", "ticker": "AMZN"},
                            {"company_ref": "company:ticker:googl", "ticker": "GOOGL"}]
    writer = type("W", (), {})()
    writer.store = type("S", (), {"connection": connection})()
    writer.coverage_mission = type("M", (), {"mission": lambda self, ref: {"universe": universe}})()
    service = DocumentExtractionService(writer)
    service._document_text = lambda context: document_text
    service._document_head = lambda context: document_text[:400]
    context = {"mission_version_ref": "m", "document_ref": "public-web-document:x",
               "source_ref": source_ref, "document_date": document_date,
               "document_date_basis": basis}
    return service, context, connection


def _suggestion(statement, *, period="2026", raw_text=None):
    return {"normalized_statement": statement, "period": period,
            "citation": {"raw_text": raw_text if raw_text is not None else statement}}


class AdmissionPathTests(unittest.TestCase):
    """The checks as ``DocumentExtractionService`` asks them before staging."""

    WEB = "source:web-search"

    def test_every_statement_from_a_statistics_page_is_held(self) -> None:
        service, context, connection = _service(source_ref=self.WEB, document_text=SEO_PAGE)
        self.addCleanup(connection.close)
        check = service.admission_quality_check(context)
        suggestion, hold = check(_suggestion(
            "Amazon's advertising business became one of its fastest-growing segments."))
        self.assertIn("statistics compilation", hold)
        # The same statement from a news page is not held for it.
        service, context, connection = _service(source_ref=self.WEB, document_text=NEWS_PAGE)
        self.addCleanup(connection.close)
        self.assertIsNone(service.admission_quality_check(context)(_suggestion(
            "Amazon said it would spend more on capital expenditures in 2026 than planned."))[1])
        # A statistics page reached as a sales note is not a fetched page.
        service, context, connection = _service(source_ref="source:sales-notes",
                                                document_text=SEO_PAGE)
        self.addCleanup(connection.close)
        self.assertIsNone(service.admission_quality_check(context)(_suggestion(
            "Amazon's advertising business became one of its fastest-growing segments."))[1])

    def test_temporal_and_relative_year_holds_and_the_anchored_rewrite(self) -> None:
        service, context, connection = _service(source_ref=self.WEB, document_text=NEWS_PAGE)
        self.addCleanup(connection.close)
        check = service.admission_quality_check(context)
        _, hold = check(_suggestion("Amazon's retail media ad spend share rose in 2026."))
        self.assertIn("unfinished_period_outcome_as_fact", hold)
        _, hold = check(_suggestion(RelativeYearTests.STATEMENT))
        self.assertIn("relative year", hold)
        service, context, connection = _service(
            source_ref="source:sales-notes", document_text=NEWS_PAGE,
            document_date="2026-08-04", basis="published")
        self.addCleanup(connection.close)
        original = _suggestion(RelativeYearTests.STATEMENT)
        staged, hold = service.admission_quality_check(context)(original)
        self.assertIsNone(hold)
        self.assertEqual(staged["period"], "2027")
        self.assertIn("(2027)", staged["normalized_statement"])
        self.assertEqual(original["period"], "2026")  # the drafted suggestion is not mutated



if __name__ == "__main__":
    unittest.main()
