"""2026-09-24 audit: deterministic repairs to qualitative Claim extraction.

* periods: "current", "upcoming earnings", "near-term" were admitted as the
  period of a Claim, which then meant nothing a week later;
* subject: the prompt invited industry, customer and competitor findings and
  filed them all under the window's company;
* citations: 3,623 of 3,685 bindings were exactly the 1,200-character slice
  the quote happened to be, cut mid-sentence;
* staleness: June morning digests were being read in September as if current.
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dalton_core.document_extraction import (
    QUOTE_CHARS,
    DocumentExtractionService,
    build_prompt,
    expand_to_sentences,
    locate_excerpt,
    normalize_period,
    parse_suggestions,
    sentence_quote_bounds,
)
from dalton_core.document_extraction_cli import run_extraction
from dalton_core.research_review import HumanReviewAuthority
from dalton_core.research_verification import ResearchVerificationError
from dalton_core.store import canonical_json
from tests.test_document_extraction_automation import AutomationDraftingTests

QUOTE_TEXT = ("Management said client decisions remain cautious.  Accenture expects\n"
              "growth to accelerate in FY26 as bookings convert. Other remarks followed.")


def _context(**extra):
    return {"quotes": [{"quote_id": "quote:100:245:abc", "source_start": 100, "source_end": 245,
                        "raw_text": QUOTE_TEXT}], **extra}


def _item(**extra):
    return {"quote_id": "quote:100:245:abc",
            "normalized_statement": "Accenture expects growth to accelerate as bookings convert.",
            "metric_or_aspect": "aspect:growth", "period": "FY26", "basis": "management commentary",
            **extra}


def _parse(items, context, tolerant=True):
    return parse_suggestions(canonical_json({"schema_version": "0.1", "suggestions": items}),
                             context, tolerant=tolerant)


class SentenceQuoteTests(unittest.TestCase):
    def test_quotes_tile_the_window_exactly_and_end_at_sentences(self) -> None:
        text = ("This is sentence number %d, which says something about demand. " * 60) % tuple(range(60))
        bounds = sentence_quote_bounds(text, 0, len(text), QUOTE_CHARS)
        self.assertEqual(bounds[0][0], 0)
        self.assertEqual(bounds[-1][1], len(text))
        for (a, b), (c, _d) in zip(bounds, bounds[1:]):
            self.assertEqual(b, c)  # no gap, no overlap
        for start, stop in bounds[:-1]:
            self.assertLessEqual(stop - start, QUOTE_CHARS)
            self.assertTrue(text[start:stop].rstrip().endswith("demand."), text[start:stop][-30:])

    def test_text_without_sentences_falls_back_to_words_then_the_hard_bound(self) -> None:
        words = "word " * 400
        bounds = sentence_quote_bounds(words, 0, len(words), 100)
        self.assertTrue(all(words[b - 1] == " " for _a, b in bounds[:-1]))
        solid = "x" * 350
        self.assertEqual(sentence_quote_bounds(solid, 0, 350, 100),
                         [(0, 100), (100, 200), (200, 300), (300, 350)])

    def test_a_decimal_point_is_not_a_sentence_end(self) -> None:
        text = "Revenue grew 3.5 percent in the quarter and margins held. " * 5
        for start, stop in sentence_quote_bounds(text, 0, len(text), 80):
            self.assertFalse(text[start:stop].rstrip().endswith("3."))

    def test_a_verbatim_excerpt_is_located_even_when_line_breaks_were_reflowed(self) -> None:
        exact = locate_excerpt(QUOTE_TEXT, "Management said client decisions remain cautious.")
        self.assertEqual(locate_excerpt(QUOTE_TEXT, "...client decisions remain cautious."),
                         (exact[0] + len("Management said "), exact[1]))
        self.assertEqual(QUOTE_TEXT[exact[0]:exact[1]], "Management said client decisions remain cautious.")
        folded = locate_excerpt(QUOTE_TEXT, "Accenture expects growth to accelerate in FY26")
        self.assertEqual(QUOTE_TEXT[folded[0]:folded[1]], "Accenture expects\ngrowth to accelerate in FY26")
        self.assertIsNone(locate_excerpt(QUOTE_TEXT, "Accenture expects growth to slow sharply"))
        left, right = expand_to_sentences(QUOTE_TEXT, *folded)
        self.assertEqual(QUOTE_TEXT[left:right],
                         "Accenture expects\ngrowth to accelerate in FY26 as bookings convert.")


class PeriodTests(unittest.TestCase):
    def test_relative_periods_are_anchored_to_the_document_date(self) -> None:
        for period in ("current", "near-term", "upcoming earnings", "Q3", "当前"):
            self.assertEqual(normalize_period(period, document_date="2026-06-10"),
                             f"{period} (as of 2026-06-10)")
        self.assertEqual(normalize_period("current", document_date="2026-09-01", date_basis="retrieved"),
                         "current (as of retrieval 2026-09-01)")

    def test_absolute_periods_are_kept_and_stale_documents_are_labelled_historical(self) -> None:
        for period in ("FY26", "Q2 2026", "2Q26", "1H26", "2026年及以后", "CY25"):
            self.assertEqual(normalize_period(period, document_date="2026-06-10"), period)
        self.assertEqual(normalize_period("FY26", document_date="2026-06-10", stale=True),
                         "FY26 (historical: document dated 2026-06-10)")
        self.assertEqual(normalize_period("near-term", document_date="2026-06-10", stale=True),
                         "near-term (as of 2026-06-10; historical)")

    def test_normalisation_is_idempotent_and_bounded(self) -> None:
        once = normalize_period("near-term", document_date="2026-06-10", stale=True)
        self.assertEqual(normalize_period(once, document_date="2026-06-10", stale=True), once)
        self.assertLessEqual(len(normalize_period("x" * 200, document_date="2026-06-10")), 200)

    def test_a_relative_period_with_no_date_is_refused(self) -> None:
        with self.assertRaisesRegex(ResearchVerificationError, "relative period"):
            normalize_period("current", document_date=None)
        self.assertEqual(normalize_period("FY26", document_date=None), "FY26")


class ParseTests(unittest.TestCase):
    def test_periods_are_normalised_only_for_contexts_that_carry_a_date(self) -> None:
        dated = _parse([_item(period="current")], _context(document_date="2026-06-10",
                                                           document_date_basis="published"))
        self.assertEqual(dated["suggestions"][0]["period"], "current (as of 2026-06-10)")
        undated = _parse([_item(period="current")], _context(document_date=None))
        self.assertEqual(undated["suggestions"], [])
        self.assertIn("relative period", undated["dropped"][0]["reason"])
        # A context built under the previous contract has no date key at all
        # and is read exactly as it was.
        legacy = _parse([_item(period="current")], _context())
        self.assertEqual(legacy["suggestions"][0]["period"], "current")

    def test_a_located_excerpt_narrows_the_citation_to_its_sentences(self) -> None:
        parsed = _parse([_item(excerpt="Accenture expects growth to accelerate in FY26")], _context())
        span = parsed["suggestions"][0]["citation_span"]
        self.assertEqual(QUOTE_TEXT[span[0] - 100:span[1] - 100],
                         "Accenture expects\ngrowth to accelerate in FY26 as bookings convert.")

    def test_an_excerpt_that_is_not_in_the_quote_drops_the_suggestion(self) -> None:
        parsed = _parse([_item(excerpt="Accenture expects growth to collapse next year")], _context())
        self.assertEqual(parsed["suggestions"], [])
        self.assertIn("not verbatim", parsed["dropped"][0]["reason"])

    def test_results_without_an_excerpt_still_parse_and_cite_the_whole_quote(self) -> None:
        parsed = _parse([_item()], _context(), tolerant=False)
        self.assertNotIn("citation_span", parsed["suggestions"][0])
        # A too-short excerpt narrows nothing.
        short = _parse([_item(excerpt="Accenture")], _context())
        self.assertNotIn("citation_span", short["suggestions"][0])

    def test_an_unknown_field_is_still_refused(self) -> None:
        parsed = _parse([_item(confidence="high")], _context())
        self.assertEqual(parsed["suggestions"], [])


class PromptTests(unittest.TestCase):
    def _context(self, **extra):
        return {"company_ref": "company:sec-cik:0001467373", "company_ticker": "ACN",
                "company_label": "Accenture (ACN)", "document_ref": "sales-note:1", "offset": 0,
                "end": 10, "quotes": [{"quote_id": "q", "raw_text": "text"}], **extra}

    def test_the_prompt_names_the_subject_demands_its_relationship_and_an_excerpt(self) -> None:
        prompt = build_prompt(self._context(document_date="2026-06-10",
                                            document_date_basis="published"))
        self.assertIn("The subject company is Accenture (ACN).", prompt)
        self.assertIn("states how it bears on the subject company", prompt)
        self.assertNotIn("its industry, its customers or its named competitors", prompt)
        self.assertIn("excerpt must be copied verbatim", prompt)
        self.assertIn("dated 2026-06-10", prompt)
        self.assertIn("never a relative term", prompt)
        self.assertNotIn("historical", prompt)

    def test_a_stale_document_is_described_as_historical(self) -> None:
        prompt = build_prompt(self._context(document_date="2026-06-10", document_date_basis="published",
                                            document_stale=True, document_age_days=106))
        self.assertIn("106 days old when it was queued: it is historical", prompt)


class DatingTests(unittest.TestCase):
    def setUp(self) -> None:
        connection = sqlite3.connect(":memory:")
        self.addCleanup(connection.close)
        connection.row_factory = sqlite3.Row
        connection.execute("CREATE TABLE document_provenance_records(document_ref TEXT, published_at TEXT)")
        connection.execute("INSERT INTO document_provenance_records VALUES('alphaengine-doc:1','2026-09-01 08:00:00')")
        writer = type("W", (), {})()
        writer.store = type("S", (), {"connection": connection})()
        self.service = DocumentExtractionService(writer)
        self.review = {"document_ref": "alphaengine-doc:9", "created_at": "2026-09-24T07:00:00+00:00"}

    def test_the_manifest_date_is_used_and_staleness_is_measured_from_the_review(self) -> None:
        dating = self.service._document_dating({"doc_date": "2026-06-10"}, self.review, None)
        self.assertEqual(dating, {"document_date": "2026-06-10", "document_date_basis": "published",
                                  "document_age_days": 106, "document_stale": True})
        fresh = self.service._document_dating({"excerpt_date": "2026-09-10"}, self.review, None)
        self.assertFalse(fresh["document_stale"])
        configured = self.service._document_dating(
            {"doc_date": "2026-06-10"}, self.review, {"stale_document_days": 200})
        self.assertFalse(configured["document_stale"])

    def test_the_provenance_row_then_the_retrieval_date_answer_when_the_manifest_does_not(self) -> None:
        dating = self.service._document_dating({}, {**self.review, "document_ref": "alphaengine-doc:1"}, None)
        self.assertEqual((dating["document_date"], dating["document_date_basis"]), ("2026-09-01", "published"))
        retrieved = self.service._document_dating({"created_at": "2026-09-20T01:00:00+00:00"}, self.review, None)
        self.assertEqual((retrieved["document_date"], retrieved["document_date_basis"], retrieved["document_stale"]),
                         ("2026-09-20", "retrieved", False))

    def test_stale_document_days_is_validated(self) -> None:
        from dalton_core.document_extraction import validate_model_config
        with self.assertRaisesRegex(ResearchVerificationError, "stale_document_days|invalid"):
            validate_model_config({"stale_document_days": 0})


class AdmissionSubjectCheckTests(unittest.TestCase):
    def _service(self, *, title=None, head="Morning digest: flows and macro."):
        connection = sqlite3.connect(":memory:")
        self.addCleanup(connection.close)
        connection.row_factory = sqlite3.Row
        connection.execute("CREATE TABLE document_provenance_records(document_ref TEXT, title TEXT)")
        if title is not None:
            connection.execute("INSERT INTO document_provenance_records VALUES('sales-note:1', ?)", (title,))
        universe = [{"company_ref": "company:ticker:amzn", "ticker": "AMZN"}]
        writer = type("W", (), {})()
        writer.store = type("S", (), {"connection": connection})()
        writer.coverage_mission = type("M", (), {"mission": lambda self, ref: {"universe": universe}})()
        plan = {"companies": {"company:ticker:amzn": {"names": ["Amazon.com", "AMZN"]}}}
        service = DocumentExtractionService(writer)
        service._document_head = lambda context: head
        return service, plan

    def _check(self, service, plan, spec_ref=None):
        with patch("dalton_core.claim_subject.writer_feed_plans", return_value=[plan]):
            return service.admission_subject_check(
                {"mission_version_ref": "m", "document_ref": "sales-note:1"}, spec_ref)

    def test_a_span_that_never_names_the_company_is_held(self) -> None:
        service, plan = self._service()
        check = self._check(service, plan)
        reason = check("company:ticker:amzn", "CoreWeave's top three customers are most of its revenue.")
        self.assertTrue(reason.startswith("held for human review"))
        self.assertIsNone(check("company:ticker:amzn", "Amazon's AWS backlog grew."))
        self.assertIsNone(check("industry:us-hyperscalers", "Power is the bottleneck."))

    def test_the_subjects_own_document_is_exempt(self) -> None:
        service, plan = self._service(title="Amazon.com Q2 2026 Earnings Call")
        self.assertIsNone(self._check(service, plan)("company:ticker:amzn", "We grew backlog."))
        service, plan = self._service(head="AMZN 2Q review: AWS reaccelerates.")
        self.assertIsNone(self._check(service, plan)("company:ticker:amzn", "We grew backlog."))
        service, plan = self._service()
        # A filing fetched by the issuer's accession is attributed by construction.
        filing = self._check(service, plan, spec_ref="annual-report-10k")
        self.assertIsNone(filing("company:ticker:amzn", "We grew backlog."))


class SubjectRejectionRuleTests(unittest.TestCase):
    def test_the_rule_lives_next_to_the_content_rejection_and_holds_rather_than_refuses(self) -> None:
        from dalton_core.research_auto_commit import document_qualitative_subject_rejection as rule

        needles = ["amazon", "amzn"]
        self.assertIn("held for human review", rule(
            subject_ref="company:ticker:amzn", cited_span="CoreWeave customers are concentrated.",
            needles=needles, document_is_own=False))
        self.assertIsNone(rule(subject_ref="company:ticker:amzn", cited_span="Amazon grew AWS.",
                               needles=needles, document_is_own=False))
        self.assertIsNone(rule(subject_ref="company:ticker:amzn", cited_span="We grew.",
                               needles=needles, document_is_own=True))
        self.assertIsNone(rule(subject_ref="industry:hyperscalers", cited_span="Power is short.",
                               needles=[], document_is_own=False))


class HeldAdmissionTests(AutomationDraftingTests):
    """End to end: a held statement is staged for a person and never committed."""

    def _run(self, suggestions, *, hold: bool):
        self._grant_automation()
        self._policy_with_document_rule()
        context = self._active_context()
        fixture = self.root / "quality-fixture.json"
        fixture.write_text(json.dumps({"schema_version": "0.1", "suggestions": [
            {**item, "quote_id": context["quotes"][0]["quote_id"]} for item in suggestions]}),
            encoding="utf-8")
        before = self.h.counts()
        checker = (lambda _self, _context, _spec: (lambda _ref, _span: "held for human review: fixture"))
        patches = [patch.object(DocumentExtractionService, "admission_subject_check", checker)] if hold else []
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        summary = run_extraction(
            state_dir=self.root, model_config_path=self._model_config(),
            summary_dir=self.root / "quality-summary", spool_dir=self.root / "spool",
            scheduler_db=self.root / "scheduler.sqlite", requested_by=None,
            max_windows=2, max_numeric_windows=0, max_discovery_windows=0,
            connector_governance=None, web_fetch_governance=None,
            hermetic_fixture=fixture, candidate_staging=self.root / "staging.sqlite",
        )
        return summary, before, context

    def test_a_held_statement_is_staged_for_review_and_the_ledger_is_untouched(self) -> None:
        summary, before, _context = self._run([{
            "normalized_statement": "Fixture management described cautious client decisions.",
            "metric_or_aspect": "aspect:client-decisions", "period": "FY26",
            "basis": "fixture management commentary"}], hold=True)
        self.assertEqual([item["status"] for item in summary["admitted"]], ["held"], summary["admitted"])
        self.assertEqual(summary["formal_authority_writes"], 0)
        after = self.h.counts()
        self.assertEqual(after["claim_versions"], before["claim_versions"])
        self.assertEqual(summary["held_candidates"], 1)
        [resolved] = summary["resolved_reviews"]
        self.assertEqual((resolved["status"], resolved["held"]), ("extraction_staged", 1))
        review = HumanReviewAuthority(self.root / "staging.sqlite")
        self.addCleanup(review.close)
        status = review.candidate_status(summary["admitted"][0]["candidate_claim_ref"])
        self.assertEqual(status["review_state"], "staged")

    def test_an_excerpt_narrows_the_committed_citation_and_the_period_is_anchored(self) -> None:
        summary, before, context = self._run([{
            "normalized_statement": "Accenture management says client decisions remain cautious.",
            "metric_or_aspect": "aspect:client-decisions", "period": "current",
            "basis": "fixture management commentary",
            "excerpt": "Accenture management says client decisions remain cautious"}], hold=False)
        [admitted] = summary["admitted"]
        self.assertEqual(admitted["status"], "admitted", admitted)
        quote = context["quotes"][0]
        self.assertGreater(admitted["source_start"], quote["source_start"] - 1)
        self.assertLess(admitted["source_end"] - admitted["source_start"], 200)
        claim = self.h.h.core.get_claim(admitted["claim_version_ref"])["claim"]
        self.assertRegex(claim["period"], r"^current \(as of (retrieval )?\d{4}-\d{2}-\d{2}")


if __name__ == "__main__":
    unittest.main()
