"""P11b: a broker's target is that broker's statement, stored as one.

The two rules worth testing hardest are the two that would be invisible if they
broke: a broker figure must never be admissible as a quantitative Claim about
the company, and the same house publishing twice must never look like two.
"""

from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path

from dalton_core.document_figure_grade import (
    ALL_GRADES,
    BROKER_RESEARCH,
    GRADES,
    GRADE_BY_SPEC,
    basis_for,
    FILED,
    SPOKEN,
    figure_worthy,
    grade_for,
    qualify,
)
from dalton_core.document_numeric_claim import (
    LABEL_ALIASES_BY_GRADE,
    NumericCandidateError,
    verify_numeric_candidate,
)
from dalton_core.research_verification import FIGURE_ADMISSIBLE_GRADES
from dalton_core.store import DaltonStore
from dalton_core.street_estimate import (
    BASIS,
    BROKERS,
    CONSENSUS_POLICY,
    CONSENSUS_POLICY_HASH,
    CONSENSUS_POLICY_REF,
    EPS_METRIC,
    SOURCE_GRADE,
    StreetEstimateConflict,
    StreetEstimateStore,
    StreetEstimateValidationError,
    TARGET_METRIC,
    broker_slug,
    normalise_rating,
    rating_scales_for,
    report_consensus,
    validate_estimate,
)

ACN = "company:sec-cik:0001467373"
MANIFEST = "9" * 64
EXTRACTOR_REF = "extractor:street-estimate-page-one:0.1"
QUOTE = (
    "Accenture PLC\nSeptember 2, 2026\nPrice: $186.53 (09/01/2026)\n"
    "Price Target: $173.00\nHOLD (2)\nModel Update\n"
)


def figure(value="173.00", metric=TARGET_METRIC, label="Price Target",
           period="12 months from 2026-09-02", quote=QUOTE, unit="currency",
           currency="USD", scale=None):
    return verify_numeric_candidate({
        "quote_id": "quote:0:1200:abc", "metric_ref": metric,
        "subject_as_named": "Accenture", "as_reported_label": label,
        "value": value, "unit": unit, "currency": currency, "period": period,
        "basis": BASIS, "scale": scale,
    }, {"quote:0:1200:abc": quote}, grade=BROKER_RESEARCH)


def estimate(**overrides):
    value = {
        "company_ref": ACN,
        "document_ref": "alphaengine-doc:1",
        "spec_ref": "sell-side-reports",
        "source_manifest_hash": MANIFEST,
        "broker": "td",
        "broker_as_named": "TD Cowen",
        "broker_basis": "document_metadata",
        "analysts": ["Bryan C. Bergin"],
        "published_on": "2026-09-02",
        "subject_as_named": "Accenture",
        "rating": {"code": "hold", "as_named": "HOLD", "scale": "buy-hold-sell",
                   "quote_id": "quote:0:1200:abc"},
        "target_price": {"value": "173.00", "currency": "USD", "horizon": None,
                         "quote_id": "quote:0:1200:abc"},
        "figures": [figure()],
        "extraction_method": "deterministic",
    }
    value.update(overrides)
    return value


class GradeTests(unittest.TestCase):
    """The grade exists to keep these figures out of every filed-figure path."""

    def test_a_broker_figure_is_never_admissible_as_a_quantitative_claim(self):
        self.assertNotIn(BROKER_RESEARCH, FIGURE_ADMISSIBLE_GRADES)

    def test_the_grade_is_not_a_mission_document_figure_grade(self):
        # coverage_mission_document_figures.source_grade has a CHECK naming
        # only the other two. Membership here would mean an insert that fails
        # at the database with a constraint error nobody can read.
        self.assertNotIn(BROKER_RESEARCH, GRADES)
        self.assertIn(BROKER_RESEARCH, ALL_GRADES)

    def test_sell_side_research_is_still_not_read_for_company_figures(self):
        # Giving the spec a grade would make figure_worthy true for broker
        # notes and turn the ordinary numeric pass loose on them.
        self.assertIsNone(grade_for("sell-side-reports"))
        self.assertFalse(figure_worthy("sell-side-reports"))
        self.assertNotIn("sell-side-reports", GRADE_BY_SPEC)

    def test_the_grade_says_what_it_is_in_a_sentence(self):
        self.assertEqual(basis_for(BROKER_RESEARCH), "broker-research-report-estimate")
        qualified = qualify("Accenture price target is 173.00 USD", BROKER_RESEARCH)
        self.assertIn("that broker's estimate", qualified)
        self.assertIn("not a figure the company published", qualified)


class LabelAliasTests(unittest.TestCase):
    """"PT" names a line on a broker note and nowhere else."""

    QUOTE = "Maintain our OP rating and $270 PT."

    def candidate(self, label="PT", basis=BASIS):
        return {
            "quote_id": "q", "metric_ref": TARGET_METRIC,
            "subject_as_named": "IBM", "as_reported_label": label,
            "value": "270", "unit": "currency", "currency": "USD",
            "period": "12 months from 2026-08-19", "basis": basis, "scale": None,
        }

    def test_the_broker_grade_opens_the_alias_table(self):
        verified = verify_numeric_candidate(
            self.candidate(), {"q": self.QUOTE}, grade=BROKER_RESEARCH
        )
        # The document's own word is what is stored; nothing is substituted in.
        self.assertEqual(verified["as_reported_label"], "PT")
        self.assertEqual(verified["label_grade"], BROKER_RESEARCH)

    def test_a_filing_that_said_PT_is_still_refused(self):
        # The owner's ruling is about broker notes. A 10-K does not print "PT",
        # so admitting it there would buy nothing and give up the guard that
        # stops a label being the amount in disguise.
        for grade in (FILED, SPOKEN):
            with self.subTest(grade=grade):
                with self.assertRaises(NumericCandidateError) as caught:
                    verify_numeric_candidate(
                        self.candidate(basis="management-reported"),
                        {"q": self.QUOTE}, grade=grade,
                    )
                self.assertIn("must name the line", str(caught.exception))

    def test_a_caller_that_names_no_grade_is_checked_as_it_always_was(self):
        with self.assertRaises(NumericCandidateError):
            verify_numeric_candidate(
                self.candidate(basis="management-reported"), {"q": self.QUOTE}
            )

    def test_a_figure_verified_without_a_grade_hashes_as_it_always_has(self):
        # Adding the key unconditionally would move the content hash of every
        # figure already stored.
        verified = verify_numeric_candidate({
            **self.candidate(label="Price Target", basis="management-reported"),
        }, {"q": "Price Target: $270.00 per share"})
        self.assertNotIn("label_grade", verified)

    def test_the_table_is_closed_and_names_only_the_broker_grade(self):
        self.assertEqual(set(LABEL_ALIASES_BY_GRADE), {BROKER_RESEARCH})
        self.assertEqual(
            LABEL_ALIASES_BY_GRADE[BROKER_RESEARCH],
            frozenset({"pt", "tp", "price target", "target price"}),
        )

    def test_an_alias_still_has_to_appear_in_the_quote(self):
        # The alias table decides whether a label names a line. It does not
        # excuse a label the document never printed.
        with self.assertRaises(NumericCandidateError):
            verify_numeric_candidate(
                self.candidate(label="TP"), {"q": self.QUOTE},
                grade=BROKER_RESEARCH,
            )


class VocabularyTests(unittest.TestCase):
    def test_a_house_is_recognised_by_its_longest_name(self):
        self.assertEqual(broker_slug("TD SECURITIES (USA) LLC"), "td")
        self.assertEqual(broker_slug("Morgan Stanley & Co. LLC"), "morgan-stanley")
        self.assertEqual(broker_slug("RBC Capital Markets"), "rbc")
        self.assertIsNone(broker_slug("A Firm Nobody Has Heard Of"))

    def test_the_slugs_agree_with_the_debate_maps(self):
        # "Two TD notes are one source" has to mean the same thing in a debate
        # and in a consensus range, or one of them is counting wrong.
        from dalton_core.debate_map import DEBATE_POLICY

        theirs = {row["publisher"] for row in DEBATE_POLICY["publishers"]}
        mine = {slug for slug, _ in BROKERS}
        self.assertTrue(
            theirs <= mine,
            f"the debate map knows houses this module does not: {sorted(theirs - mine)}",
        )

    def test_each_house_maps_its_own_words(self):
        self.assertEqual(normalise_rating("Overweight", broker="morgan-stanley")["code"], "buy")
        self.assertEqual(normalise_rating("Equal-Weight", broker="morgan-stanley")["code"], "hold")
        self.assertEqual(normalise_rating("OP", broker="rbc")["code"], "buy")
        self.assertEqual(normalise_rating("Sector Perform", broker="rbc")["code"], "hold")
        self.assertEqual(normalise_rating("HOLD", broker="td")["code"], "hold")
        self.assertEqual(normalise_rating("Neutral", broker="jpmorgan")["code"], "hold")

    def test_a_word_from_another_houses_scale_is_refused(self):
        # RBC has no Overweight rung. "Overweight" in an RBC note is a sentence
        # about somebody else's rating, and recording it would file another
        # house's view under RBC's name.
        with self.assertRaises(StreetEstimateValidationError) as caught:
            normalise_rating("Overweight", broker="rbc")
        self.assertIn("another house", str(caught.exception))

    def test_a_house_with_no_declared_scale_refuses_rather_than_guesses(self):
        self.assertEqual(rating_scales_for("scotiabank"), ())
        with self.assertRaises(StreetEstimateValidationError):
            normalise_rating("Sector Outperform", broker="scotiabank")

    def test_the_rating_keeps_the_word_the_broker_used(self):
        rating = normalise_rating("Equal-Weight", broker="morgan-stanley")
        self.assertEqual(rating["as_named"], "Equal-Weight")
        self.assertEqual(rating["scale"], "overweight")


class ValidationTests(unittest.TestCase):
    def test_a_figure_that_did_not_go_through_the_verifier_is_refused(self):
        raw = dict(figure())
        raw.pop("citation_hash")
        with self.assertRaises(StreetEstimateValidationError) as caught:
            validate_estimate(estimate(figures=[raw]))
        self.assertIn("verify_numeric_candidate", str(caught.exception))

    def test_a_figure_filed_as_the_companys_own_is_refused(self):
        wrong = verify_numeric_candidate({
            "quote_id": "q", "metric_ref": TARGET_METRIC,
            "subject_as_named": "Accenture", "as_reported_label": "Price Target",
            "value": "173.00", "unit": "currency", "currency": "USD",
            "period": "12 months from 2026-09-02", "basis": "management-reported",
            "scale": None,
        }, {"q": QUOTE})
        with self.assertRaises(StreetEstimateValidationError) as caught:
            validate_estimate(estimate(figures=[wrong]))
        self.assertIn("broker-estimate", str(caught.exception))

    def test_a_target_price_must_carry_a_currency(self):
        with self.assertRaises(StreetEstimateValidationError):
            validate_estimate(estimate(target_price={
                "value": "173.00", "currency": "", "horizon": None,
                "quote_id": "quote:0:1200:abc",
            }))

    def test_a_target_price_must_be_among_the_verified_figures(self):
        with self.assertRaises(StreetEstimateValidationError) as caught:
            validate_estimate(estimate(figures=[
                figure(value="14.65", metric=EPS_METRIC,
                       label="EPS estimate", period="FY2027",
                       quote="Our FY2027 EPS estimate is $14.65."),
            ]))
        self.assertIn("digit check", str(caught.exception))

    def test_an_unknown_house_is_not_a_street_estimate(self):
        with self.assertRaises(StreetEstimateValidationError):
            validate_estimate(estimate(broker="somebody"))

    def test_a_rating_scale_the_house_does_not_run_is_refused(self):
        with self.assertRaises(StreetEstimateValidationError):
            validate_estimate(estimate(rating={
                "code": "buy", "as_named": "Outperform", "scale": "outperform",
                "quote_id": "quote:0:1200:abc",
            }))

    def test_a_document_of_another_kind_is_refused(self):
        with self.assertRaises(StreetEstimateValidationError):
            validate_estimate(estimate(spec_ref="earnings-call-transcripts"))

    def test_a_note_with_no_verified_figure_is_not_an_estimate(self):
        with self.assertRaises(StreetEstimateValidationError):
            validate_estimate(estimate(figures=[], target_price=None))


class StoreTestCase(unittest.TestCase):
    def setUp(self):
        self._dir = tempfile.TemporaryDirectory()
        self.addCleanup(self._dir.cleanup)
        self.store = DaltonStore(str(Path(self._dir.name) / "core.sqlite"))
        self.addCleanup(self.store.close)
        self.estimates = StreetEstimateStore(self.store)


class StoreTests(StoreTestCase):
    def test_a_note_is_stored_with_its_grade_and_its_sentence(self):
        recorded = self.estimates.record(estimate())
        self.assertEqual(recorded["status"], "fresh")
        self.assertEqual(recorded["source_grade"], SOURCE_GRADE)
        self.assertIn("that broker's estimate", recorded["statement"])
        self.assertIn("TD Cowen", recorded["statement"])

    def test_reading_the_same_note_twice_stores_it_once(self):
        first = self.estimates.record(estimate())
        again = self.estimates.record(estimate())
        self.assertEqual(again["status"], "duplicate")
        self.assertEqual(again["id"], first["id"])
        self.assertEqual(len(self.estimates.estimates(ACN)), 1)

    def test_rows_are_append_only(self):
        self.estimates.record(estimate())
        for statement in (
            "UPDATE street_estimates SET target_value='1'",
            "DELETE FROM street_estimates",
        ):
            with self.assertRaises(sqlite3.DatabaseError):
                with self.store._transaction() as cur:
                    cur.execute(statement)

    def test_an_unauthorised_connection_cannot_insert(self):
        self.estimates.record(estimate())
        raw = sqlite3.connect(str(Path(self._dir.name) / "core.sqlite"))
        self.addCleanup(raw.close)
        with self.assertRaises(sqlite3.DatabaseError):
            raw.execute(
                "INSERT INTO street_estimates(estimate_id,company_ref,document_ref,"
                "spec_ref,source_manifest_hash,broker,broker_as_named,broker_basis,"
                "analysts_json,published_on,source_grade,extraction_method,"
                "record_json,content_hash,observed_by,created_at) "
                "VALUES('x',?,'d','sell-side-reports',?,'td','TD','document_metadata',"
                "'[]','2026-09-02','broker-research-report','deterministic','{}',"
                "'h','o','t')",
                (ACN, MANIFEST),
            )
            raw.commit()

    def test_a_scan_records_why_it_found_nothing(self):
        self.estimates.record_scan(
            company_ref=ACN, document_ref="alphaengine-doc:9", outcome="refused",
            reason="multi_company_report", extraction_method="deterministic",
            extractor_ref=EXTRACTOR_REF,
        )
        self.assertEqual(
            self.estimates.scanned_document_refs(ACN), {"alphaengine-doc:9"}
        )
        self.assertEqual(self.estimates.scans(ACN)[0]["reason"], "multi_company_report")

    def test_a_refused_scan_must_say_why(self):
        with self.assertRaises(StreetEstimateValidationError):
            self.estimates.record_scan(
                company_ref=ACN, document_ref="d", outcome="refused",
                extraction_method="deterministic", extractor_ref=EXTRACTOR_REF,
            )

    def test_a_refusal_names_the_reader_that_made_it(self):
        # So a note refused by a reader that has since been fixed can be given
        # to the new one. Without it the ledger's "never read twice" rule makes
        # every fix to the extractor unreachable.
        self.estimates.record_scan(
            company_ref=ACN, document_ref="alphaengine-doc:1", outcome="refused",
            reason="label_does_not_name_a_line",
            extraction_method="deterministic", extractor_ref="extractor:old:0.1",
        )
        self.estimates.record_scan(
            company_ref=ACN, document_ref="alphaengine-doc:2", outcome="refused",
            reason="multi_company_report",
            extraction_method="deterministic", extractor_ref=EXTRACTOR_REF,
        )
        self.assertEqual(
            self.estimates.refused_by(EXTRACTOR_REF), {"alphaengine-doc:1"}
        )

    def test_scanning_the_same_document_twice_is_a_duplicate(self):
        self.estimates.record_scan(
            company_ref=ACN, document_ref="d", outcome="refused",
            reason="no_target_price", extraction_method="deterministic",
            extractor_ref=EXTRACTOR_REF,
        )
        again = self.estimates.record_scan(
            company_ref=ACN, document_ref="d", outcome="refused",
            reason="no_target_price", extraction_method="deterministic",
            extractor_ref=EXTRACTOR_REF,
        )
        self.assertEqual(again["status"], "duplicate")


class CrossVerificationTests(StoreTestCase):
    """One broker is never a consensus, whatever it publishes."""

    def note(self, broker, value, document, published_on="2026-09-02", name=None):
        return self.estimates.record(estimate(
            broker=broker, broker_as_named=name or broker, document_ref=document,
            published_on=published_on,
            rating=None,
            target_price={"value": value, "currency": "USD", "horizon": None,
                          "quote_id": "quote:0:1200:abc"},
            figures=[figure(value=value, quote=QUOTE.replace("173.00", value))],
        ))

    def test_the_same_house_twice_is_not_a_consensus(self):
        # DXC, exactly: its only two live targets are both TD Cowen's.
        self.note("td", "11.00", "alphaengine-doc:1")
        self.note("td", "11.00", "alphaengine-doc:2")
        self.assertIsNone(
            report_consensus(self.estimates.estimates(ACN), as_of="2026-09-09")
        )

    def test_two_independent_houses_make_a_range(self):
        self.note("td", "173.00", "alphaengine-doc:1")
        self.note("wells-fargo", "194.00", "alphaengine-doc:2", name="Wells Fargo")
        block = report_consensus(self.estimates.estimates(ACN), as_of="2026-09-09")
        self.assertEqual(block["broker_count"], 2)
        self.assertEqual(block["brokers"], ["td", "wells-fargo"])
        self.assertEqual(block["low"], "173.00")
        self.assertEqual(block["high"], "194.00")
        self.assertEqual(block["mean"], "183.5")
        self.assertEqual(block["policy_ref"], CONSENSUS_POLICY_REF)
        self.assertEqual(block["policy_hash"], CONSENSUS_POLICY_HASH)
        self.assertEqual(len(block["document_refs"]), 2)

    def test_a_house_publishing_twice_contributes_its_newest_note(self):
        self.note("td", "151.00", "alphaengine-doc:1", published_on="2026-07-01")
        self.note("td", "173.00", "alphaengine-doc:2", published_on="2026-09-02")
        self.note("wells-fargo", "194.00", "alphaengine-doc:3", name="Wells Fargo")
        block = report_consensus(self.estimates.estimates(ACN), as_of="2026-09-09")
        self.assertEqual(block["broker_count"], 2)
        self.assertEqual(block["low"], "173.00")

    def test_a_target_older_than_the_window_does_not_count(self):
        self.note("td", "173.00", "alphaengine-doc:1", published_on="2026-01-05")
        self.note("wells-fargo", "194.00", "alphaengine-doc:2", name="Wells Fargo")
        self.assertIsNone(
            report_consensus(self.estimates.estimates(ACN), as_of="2026-09-09")
        )

    def test_two_currencies_do_not_make_a_range(self):
        self.note("td", "173.00", "alphaengine-doc:1")
        self.estimates.record(estimate(
            broker="hsbc", broker_as_named="HSBC", document_ref="alphaengine-doc:2",
            rating=None,
            target_price={"value": "160.00", "currency": "EUR", "horizon": None,
                          "quote_id": "quote:0:1200:abc"},
            figures=[figure(value="160.00", currency="EUR",
                            quote=QUOTE.replace("173.00", "160.00"))],
        ))
        self.assertIsNone(
            report_consensus(self.estimates.estimates(ACN), as_of="2026-09-09")
        )

    def test_the_rule_is_the_one_the_policy_says_it_is(self):
        self.assertEqual(CONSENSUS_POLICY["min_independent_brokers"], 2)
        self.assertEqual(CONSENSUS_POLICY["counted_by"], "broker")


if __name__ == "__main__":
    unittest.main()
