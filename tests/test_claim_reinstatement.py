"""2026-09-24 audit: retirements the span rule got wrong, and the way back.

The v2 span detector ran before the alias table knew AWS, and before it looked
past the span: ws-7d retired "AWS holds the leading global cloud market
share ..." as not about Amazon, "CEO Zuckerberg told employees ..." as not
about Meta, and "NDRC ... ordered the parties to withdraw" as not about Meta
when the line before the span says "NDRC orders Meta to unwind".  A retirement
is append-only, so the way back is a further record -- a reinstatement -- and
every read path treats a Claim as retired only while none exists.
"""

from __future__ import annotations

import json
import sqlite3
import unittest
from unittest.mock import patch

from dalton_core import claim_retirement
from dalton_core.claim_retirement import (
    REREVIEW_RULE_REF,
    SPAN_V2_DETECTOR_REF,
    ClaimRetirementConflict,
    ClaimRetirementNotFound,
    ClaimRetirementValidationError,
    reinstated_claim_version_refs,
    retired_claim_version_refs,
    retirement_state_probe,
)
from dalton_core.claim_subject import (
    citation_context_names_subject,
    executive_needles,
    own_document_evidence,
    span_names_subject_for_admission,
    subject_absent_from_citation,
    text_names_word,
)
from tests.test_claim_retirement import ClaimRetirementHarness

AMZN = ["amazon", "amazon web services", "amazon.com", "amzn", "aws", "web"]
META = ["facebook", "meta", "meta platforms"]
GOOGL = ["alphabet", "googl", "google"]
PEERS_OF_META = ["amazon", "amzn", "aws", "alphabet", "googl", "google", "microsoft", "msft"]

NDRC_BEFORE = ("(CAICT)  Tech/AI: NDRC orders Meta to unwind USD2bn purchase of AI "
               "startup ManusAccording to the Office ")
NDRC_SPAN = ("of the Security Review Working Mechanism for Foreign Investment under the "
             "NDRC, it requires the parties involved to withdraw the acquisition transaction")
NDRC_STATEMENT = ("China's NDRC prohibited foreign investment in AI startup Manus and "
                  "ordered the parties to withdraw the acquisition.")


class RetirementRuleTests(unittest.TestCase):
    def test_a_name_is_a_whole_word_and_a_weak_token_is_no_name(self) -> None:
        self.assertFalse(text_names_word("new laws and draws", AMZN))
        self.assertFalse(text_names_word("the website traffic", AMZN))
        self.assertTrue(text_names_word("AWS's historical ramp", AMZN))
        self.assertTrue(text_names_word("亚马逊整体 EBIT", ["亚马逊"]))
        self.assertFalse(text_names_word("metadata and metals", META))

    def test_an_executive_keeps_a_claim_but_is_not_an_alias(self) -> None:
        statement = "Management commentary from Jassy walked investors through ROIC."
        self.assertIn("jassy", executive_needles(AMZN))
        self.assertEqual(executive_needles(["someco"]), [])
        self.assertFalse(subject_absent_from_citation(
            span="He walked through ROIC and capex.", statement=statement,
            needles=AMZN, document_is_own=False))
        # Admission is untouched: a person's name does not name the issuer.
        self.assertFalse(span_names_subject_for_admission(
            span="Jassy walked through ROIC.", needles=AMZN, document_is_own=False))

    def test_the_antecedent_before_the_span_keeps_a_statement_that_leans_on_it(self) -> None:
        self.assertFalse(subject_absent_from_citation(
            span=NDRC_SPAN, statement=NDRC_STATEMENT, needles=META,
            document_is_own=False, context_before=NDRC_BEFORE, context_after="",
            peer_needles=PEERS_OF_META))
        # Without the context the v2 rule still retires it.
        self.assertTrue(subject_absent_from_citation(
            span=NDRC_SPAN, statement=NDRC_STATEMENT, needles=META,
            document_is_own=False))

    def test_a_digest_neighbour_does_not_keep_a_statement_that_stands_alone(self) -> None:
        # The sell-side digest case: META named in the line before, the
        # statement an industry fact with nothing that refers back to it.
        statement = "Memory, not compute, is now the defining constraint on AI systems."
        self.assertTrue(subject_absent_from_citation(
            span="A Hot Chips takeaway: the memory wall limits HBM scaling.",
            statement=statement, needles=META, document_is_own=False,
            context_before="META: legal expert takeaways, long process ahead. ",
            context_after="", peer_needles=PEERS_OF_META))

    def test_a_nearer_peer_or_a_peer_in_the_statement_wins(self) -> None:
        self.assertFalse(citation_context_names_subject(
            statement="Management guided capex higher.", span="Capex guide raised.",
            before="Meta said capex would rise. Then Alphabet reported. ", after="",
            needles=META, peer_needles=PEERS_OF_META))
        self.assertFalse(citation_context_names_subject(
            statement="Management at NVDA said the company shares upside.",
            span="Vendor financing.", before="Meta ", after="",
            needles=META, peer_needles=PEERS_OF_META))

    def test_a_filing_cover_or_density_makes_the_document_the_subjects_own(self) -> None:
        xbrl = "goog-20251231  FALSE 2025 FY 0001652044 P7Y http://fasb.org/us-gaap/2025#Revenues " * 3
        self.assertEqual(own_document_evidence(text=xbrl, needles=GOOGL), "xbrl_cover")
        cik = "0001352010 10-Q report " + "filler " * 100
        self.assertEqual(own_document_evidence(
            text=cik, needles=["epam"], subject_ref="company:sec-cik:0001352010"), "cik_cover")
        cover = "UNITED STATES SECURITIES AND EXCHANGE COMMISSION FORM 10-K Alphabet Inc."
        self.assertEqual(own_document_evidence(text="x" * 500 + cover, needles=GOOGL),
                         "filing_cover")
        own = "Alphabet reported. " * 40
        self.assertEqual(own_document_evidence(text="x" * 500 + own, needles=GOOGL,
                                               peer_needles=["amazon"]), "density")
        digest = ("Alphabet reported. Amazon reported. Microsoft reported. " * 30)
        self.assertIsNone(own_document_evidence(
            text="x" * 500 + digest, needles=GOOGL, peer_needles=["amazon", "microsoft"]))
        self.assertIsNone(own_document_evidence(text="metadata about metals " * 20,
                                                needles=META))


class _V2Retirement(ClaimRetirementHarness):
    """A retirement made the way ws-7d's were: detector v2, span rationale."""

    def retire_v2(self, claim: dict[str, str], *, cited_span: str,
                  needles: list[str], source: str) -> dict:
        with patch.dict(claim_retirement.DETECTOR_REFS,
                        {"subject_absent_from_source": SPAN_V2_DETECTOR_REF}):
            challenge = self.authority.challenge(
                claim_version_ref=claim["ref"], claim_version_hash=claim["hash"],
                reason_code="subject_absent_from_source",
                rationale="这条结论所引的原文片段（1,200 字）和结论本身都没有出现 x",
                actor_ref="automation:coverage-mission")
        return self.authority.decide(
            challenge_ref=challenge["id"], challenge_hash=challenge["content_hash"],
            decision="retired", actor_ref="automation:coverage-mission", rationale="r",
            subject_needles=needles, source_text=source, cited_span=cited_span)


AWS_DOC = ("Morning digest. Automakers see power generation demand. "
           + "Filler about macro flows. " * 20
           + "AWS holds the leading global cloud market share among providers.")
AWS_STATEMENT = "AWS holds the leading global cloud market share."


class ReinstatementAuthorityTests(_V2Retirement):
    def _retired(self) -> dict[str, str]:
        start = AWS_DOC.index("AWS holds")
        claim = self.claim(statement=AWS_STATEMENT, source=AWS_DOC,
                           span=(start, len(AWS_DOC)))
        # Retired under the pre-alias needles: amazon only.
        self.retire_v2(claim, cited_span=AWS_DOC[start:], needles=["amazon"],
                       source=AWS_DOC)
        return claim

    def test_a_person_withdraws_a_retirement_by_appending_a_record(self) -> None:
        claim = self._retired()
        before = self.store.connection.execute(
            "SELECT record_json, content_hash FROM claim_retirement_decisions").fetchall()
        self.assertIn(claim["ref"], retired_claim_version_refs(self.store.connection))
        probe = retirement_state_probe(self.store.connection)
        with self.assertRaises(ClaimRetirementValidationError):
            self.authority.reinstate(claim_version_ref=claim["ref"], actor_ref="nobody",
                                     rationale="r")
        with self.assertRaises(ClaimRetirementConflict):
            self.authority.reinstate(claim_version_ref=claim["ref"], actor_ref="human:lumos",
                                     rationale="r", decision_hash="0" * 64)
        record = self.authority.reinstate(
            claim_version_ref=claim["ref"], actor_ref="human:lumos",
            rationale="原文说的就是 AWS")
        self.assertEqual(record["status"], "fresh")
        self.assertEqual(record["reason_code"], "human_judgment")
        # The retirement is untouched, byte for byte.
        self.assertEqual(before, self.store.connection.execute(
            "SELECT record_json, content_hash FROM claim_retirement_decisions").fetchall())
        self.assertNotIn(claim["ref"], retired_claim_version_refs(self.store.connection))
        self.assertIn(claim["ref"], reinstated_claim_version_refs(self.store.connection))
        self.assertNotIn(claim["ref"], self.authority.retired_claim_version_refs())
        self.assertNotEqual(probe, retirement_state_probe(self.store.connection))
        again = self.authority.reinstate(
            claim_version_ref=claim["ref"], actor_ref="human:lumos", rationale="again")
        self.assertEqual(again["status"], "duplicate")
        for sql in ("UPDATE claim_retirement_reinstatements SET rationale='x'",
                    "DELETE FROM claim_retirement_reinstatements"):
            with self.assertRaises(sqlite3.DatabaseError):
                self.store.connection.execute(sql)

    def test_automation_reinstates_only_when_the_current_rule_no_longer_fires(self) -> None:
        claim = self._retired()
        start = AWS_DOC.index("AWS holds")
        with self.assertRaises(ClaimRetirementConflict):
            # The old needles: the rule still fires.
            self.authority.reinstate(
                claim_version_ref=claim["ref"], actor_ref="automation:coverage-mission",
                rationale="r", subject_needles=["amazon"], source_text=AWS_DOC,
                cited_span=AWS_DOC[start:])
        with self.assertRaises(ClaimRetirementConflict):
            # Never unverified.
            self.authority.reinstate(
                claim_version_ref=claim["ref"], actor_ref="automation:coverage-mission",
                rationale="r", subject_needles=AMZN)
        record = self.authority.reinstate(
            claim_version_ref=claim["ref"], actor_ref="automation:coverage-mission",
            rationale="r", subject_needles=AMZN, source_text=AWS_DOC,
            cited_span=AWS_DOC[start:])
        self.assertEqual(record["reason_code"], "subject_named_under_current_rule")
        self.assertEqual(record["rule_ref"], REREVIEW_RULE_REF)
        self.assertEqual(record["retired_detector_ref"], SPAN_V2_DETECTOR_REF)

    def test_automation_never_withdraws_another_kind_of_retirement(self) -> None:
        claim = self.claim(statement="Past performance is not indicative of future results.",
                           source="Past performance is not indicative of future results.")
        challenge = self.authority.challenge(
            claim_version_ref=claim["ref"], claim_version_hash=claim["hash"],
            reason_code="boilerplate_disclaimer", rationale="套话",
            actor_ref="automation:coverage-mission")
        self.authority.decide(
            challenge_ref=challenge["id"], challenge_hash=challenge["content_hash"],
            decision="retired", actor_ref="automation:coverage-mission", rationale="r")
        with self.assertRaises(ClaimRetirementConflict):
            self.authority.reinstate(
                claim_version_ref=claim["ref"], actor_ref="automation:coverage-mission",
                rationale="r", subject_needles=AMZN, source_text="x", cited_span="x")
        with self.assertRaises(ClaimRetirementNotFound):
            self.authority.reinstate(claim_version_ref="claim-version:unknown",
                                     actor_ref="human:lumos", rationale="r")


class RereviewTests(_V2Retirement):
    def _driver(self, needles):
        from dalton_core.claim_review import ClaimReviewDriver

        return ClaimReviewDriver(
            store=self.store, missions=self.missions, challenges=self.authority,
            spool=self.spool, needles={self.subject: needles})

    def setUp(self) -> None:
        super().setUp()
        from tests.test_claim_retirement import EPAM
        self.subject = EPAM
        self.doc = ("Digest. " + "Filler about macro flows. " * 20
                    + "Bench strength: EPAM Engineering Services grew bookings. "
                    + "Automakers see power generation as a near-term opportunity.")
        good_start = self.doc.index("Bench strength")
        wrong_start = self.doc.index("Automakers")
        # Retired under v2 because the needles then were ["epamx"]: a name
        # table that did not know what the company is called.
        # v4 (2026-09-25): the statement itself names the company; a span
        # that merely contains the name no longer puts a Claim back.
        self.fixed = self.claim(statement="EPAM Engineering Services grew bookings.",
                                source=self.doc, span=(good_start, wrong_start))
        self.retire_v2(self.fixed, cited_span=self.doc[good_start:wrong_start],
                       needles=["epamx"], source=self.doc)
        self.right = self.claim(statement="Automakers see power generation demand.",
                                source=self.doc + " ", span=(wrong_start, len(self.doc)))
        self.retire_v2(self.right, cited_span=self.doc[wrong_start:],
                       needles=["epamx"], source=self.doc + " ")

    def test_rereview_is_bounded_idempotent_and_only_touches_v2_span_retirements(self) -> None:
        self.grant_claim_challenge()
        summary = self._driver(["epam"]).run_once()
        rereview = summary["rereview"]
        self.assertEqual(rereview["candidates"], 2)
        self.assertEqual([item["claim_version_ref"] for item in rereview["reinstated"]],
                         [self.fixed["ref"]])
        self.assertEqual(rereview["still_retired"], 1)
        retired = retired_claim_version_refs(self.store.connection)
        self.assertNotIn(self.fixed["ref"], retired)
        self.assertIn(self.right["ref"], retired)
        # A second tick reads nothing and writes nothing.
        reads = self.spool.reads
        again = self._driver(["epam"]).run_once()["rereview"]
        self.assertEqual(again["candidates"], 1)
        self.assertEqual(again["already_reviewed"], 1)
        self.assertEqual(again["reinstated"], [])
        self.assertEqual(self.spool.reads, reads)
        self.assertEqual(len(self.authority.reinstatements()), 1)
        # A changed alias table re-judges the still-retired one once.
        third = self._driver(["epam", "automakers"]).run_once()["rereview"]
        self.assertEqual([item["claim_version_ref"] for item in third["reinstated"]],
                         [self.right["ref"]])

    def test_without_the_grant_it_only_reports(self) -> None:
        summary = self._driver(["epam"]).run_once()
        self.assertEqual(summary["rereview"]["reinstated"], [])
        self.assertEqual([item["claim_version_ref"]
                          for item in summary["rereview"]["would_reinstate"]],
                         [self.fixed["ref"]])
        self.assertEqual(reinstated_claim_version_refs(self.store.connection), set())

    def test_the_read_only_driver_dry_runs_over_a_mode_ro_connection(self) -> None:
        import tempfile
        from pathlib import Path

        from dalton_core.claim_review import ClaimReviewDriver

        with tempfile.TemporaryDirectory() as temp:
            path = Path(temp) / "core.sqlite"
            backup = sqlite3.connect(path)
            self.store.connection.backup(backup)
            backup.close()
            connection = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
            connection.row_factory = sqlite3.Row
            self.addCleanup(connection.close)
            driver = ClaimReviewDriver.read_only(
                connection=connection, missions=self.missions, spool=self.spool,
                needles={self.subject: ["epam"]})
            summary = driver.rereview_retirements(principal=None, dry_run=True)
        self.assertEqual([item["claim_version_ref"] for item in summary["would_reinstate"]],
                         [self.fixed["ref"]])


class StrictReinstatementRuleTests(unittest.TestCase):
    """2026-09-25 audit: what may put a retired Claim back (rule v4)."""

    GOOGL_ALL = ["alphabet", "gemini", "goog", "googl", "google"]
    PEERS_OF_GOOGL = ["amazon", "aws", "meta", "facebook", "microsoft", "msft"]

    def named(self, statement, span="A long digest window mentioning GOOG somewhere.", **kw):
        from dalton_core.claim_subject import subject_named_for_reinstatement

        return subject_named_for_reinstatement(
            statement=statement, span=span, needles=kw.pop("needles", self.GOOGL_ALL),
            peer_needles=kw.pop("peers", self.PEERS_OF_GOOGL), **kw)

    def test_a_name_somewhere_in_the_span_is_not_a_reason(self) -> None:
        # 4d4e7f46: an expert on software moats, a GOOG in the 1,200-char window.
        self.assertIsNone(self.named(
            "An expert cited by SignalFire argues code is no longer a moat for software."))

    def test_a_statement_about_a_peer_is_the_peers(self) -> None:
        # 5f5b205f: filed under GOOGL, about META.
        self.assertIsNone(self.named(
            "The expert adds it is too early to count META out as long as they have "
            "access to compute."))
        # 14cf6141 (AMZN): about GOOGL and META, an AWS somewhere in the span.
        self.assertIsNone(self.named(
            "Per the note, the FT reported GOOGL capped META's use of Gemini AI.",
            span="... AWS ...", needles=AMZN, peers=["googl", "gemini", "meta"]))

    def test_the_statement_its_executive_or_its_antecedent_is(self) -> None:
        self.assertEqual(self.named("The same expert says GOOG continues to benefit."),
                         "statement")
        self.assertEqual(self.named("Sundar Pichai said capacity is tight.", span="x"),
                         "executive")
        self.assertEqual(self.named(
            NDRC_STATEMENT, span=NDRC_SPAN, needles=META, peers=PEERS_OF_META,
            context_before=NDRC_BEFORE, context_after=""), "context")

    def test_a_document_is_the_subjects_own_by_more_than_its_head(self) -> None:
        listicle = "Top 35 Social Media Platforms (January 2026) Facebook Instagram " + "x " * 400
        self.assertEqual(own_document_evidence(text=listicle, needles=META), "head")
        self.assertIsNone(own_document_evidence(text=listicle, needles=META,
                                                include_head=False))
        # 96c222bb: a statement about a third-party site's methodology.
        self.assertIsNone(self.named(
            "The document frame is a traffic-analytics article from a third-party site.",
            needles=META, peers=GOOGL, own_document=None))
        self.assertEqual(self.named("Deferred tax assets declined.", span="x",
                                    own_document="xbrl_cover"), "own_document:xbrl_cover")
        self.assertIsNone(self.named("Deferred tax assets declined.", span="x",
                                     own_document="head"))


class ReinstatementWithdrawalTests(_V2Retirement):
    """A wrong automatic reinstatement is withdrawn by a further record."""

    V3 = "claim-rereview:subject-absent-span:v3"

    def setUp(self) -> None:
        super().setUp()
        from tests.test_claim_retirement import EPAM

        self.subject = EPAM
        self.doc = ("Digest. " + "Filler about macro flows. " * 20
                    + "Bench strength: EPAM grew bookings. Peers: Accenture held share. "
                    + "Automakers see power generation as a near-term opportunity.")
        start = self.doc.index("Bench strength")
        end = self.doc.index("Automakers")
        self.span = self.doc[start:end]
        # Loose (span only) and good (the statement names EPAM), both retired
        # under v2 with a name table that did not know EPAM.
        self.loose = self.claim(statement="Bookings grew on engineering demand.",
                                source=self.doc, span=(start, end))
        self.good = self.claim(statement="EPAM bookings grew on engineering demand.",
                               source=self.doc + " ", span=(start, end))
        for claim, source in ((self.loose, self.doc), (self.good, self.doc + " ")):
            self.retire_v2(claim, cited_span=self.span, needles=["epamx"], source=source)
            self._reinstate_v3(claim, source)

    def _reinstate_v3(self, claim, source) -> dict:
        """A reinstatement made the way the v3 re-review made them."""

        with patch.object(claim_retirement, "REREVIEW_RULE_REF", self.V3), \
                patch("dalton_core.claim_subject.subject_named_for_reinstatement",
                      return_value="span"):
            return self.authority.reinstate(
                claim_version_ref=claim["ref"], actor_ref="automation:coverage-mission",
                rationale="v3", subject_needles=["epam"], source_text=source,
                cited_span=self.span)

    def _driver(self):
        from dalton_core.claim_review import ClaimReviewDriver

        return ClaimReviewDriver(store=self.store, missions=self.missions,
                                 challenges=self.authority, spool=self.spool,
                                 needles={self.subject: ["epam"]})

    def test_v4_never_reinstates_on_a_span_alone(self) -> None:
        other = self.claim(statement="Bookings grew on engineering demand again.",
                           source=self.doc + "  ", span=(self.doc.index("Bench"),
                                                         self.doc.index("Automakers")))
        self.retire_v2(other, cited_span=self.span, needles=["epamx"], source=self.doc + "  ")
        with self.assertRaises(ClaimRetirementConflict):
            self.authority.reinstate(
                claim_version_ref=other["ref"], actor_ref="automation:coverage-mission",
                rationale="r", subject_needles=["epam"], source_text=self.doc + "  ",
                cited_span=self.span)

    def test_a_withdrawal_restores_the_retirement_by_appending(self) -> None:
        connection = self.store.connection
        self.assertNotIn(self.loose["ref"], retired_claim_version_refs(connection))
        probe = retirement_state_probe(connection)
        with self.assertRaises(ClaimRetirementConflict):  # the good one is still named
            self.authority.withdraw_reinstatement(
                claim_version_ref=self.good["ref"], actor_ref="automation:coverage-mission",
                rationale="r", subject_needles=["epam"], cited_span=self.span)
        with self.assertRaises(ClaimRetirementConflict):  # never unverified
            self.authority.withdraw_reinstatement(
                claim_version_ref=self.loose["ref"], actor_ref="automation:coverage-mission",
                rationale="r", subject_needles=["epam"])
        record = self.authority.withdraw_reinstatement(
            claim_version_ref=self.loose["ref"], actor_ref="automation:coverage-mission",
            rationale="r", subject_needles=["epam"], cited_span=self.span,
            peer_needles=["accenture"])
        self.assertEqual(record["reason_code"], "subject_not_named_under_strict_rule")
        self.assertEqual(record["rule_ref"], REREVIEW_RULE_REF)
        self.assertEqual(record["reinstated_rule_ref"], self.V3)
        self.assertIn(self.loose["ref"], retired_claim_version_refs(connection))
        self.assertNotIn(self.loose["ref"], reinstated_claim_version_refs(connection))
        self.assertNotEqual(probe, retirement_state_probe(connection))
        again = self.authority.withdraw_reinstatement(
            claim_version_ref=self.loose["ref"], actor_ref="human:lumos", rationale="again")
        self.assertEqual(again["status"], "duplicate")
        for sql in ("UPDATE claim_retirement_reinstatement_withdrawals SET rationale='x'",
                    "DELETE FROM claim_retirement_reinstatement_withdrawals"):
            with self.assertRaises(sqlite3.DatabaseError):
                connection.execute(sql)
        # A person may withdraw any reinstatement, the good one included.
        self.authority.withdraw_reinstatement(
            claim_version_ref=self.good["ref"], actor_ref="human:lumos", rationale="我看过了")
        self.assertIn(self.good["ref"], retired_claim_version_refs(connection))

    def test_automation_withdraws_only_prior_rule_automatic_reinstatements(self) -> None:
        human = self.claim(statement="Bookings grew on engineering demand, per the note.",
                           source=self.doc + "   ", span=(self.doc.index("Bench"),
                                                          self.doc.index("Automakers")))
        self.retire_v2(human, cited_span=self.span, needles=["epamx"], source=self.doc + "   ")
        self.authority.reinstate(claim_version_ref=human["ref"], actor_ref="human:lumos",
                                 rationale="r")
        with self.assertRaises(ClaimRetirementConflict):
            self.authority.withdraw_reinstatement(
                claim_version_ref=human["ref"], actor_ref="automation:coverage-mission",
                rationale="r", subject_needles=["epam"], cited_span=self.span)
        with self.assertRaises(ClaimRetirementNotFound):
            self.authority.withdraw_reinstatement(
                claim_version_ref="claim-version:unknown", actor_ref="human:lumos",
                rationale="r")

    def test_the_recheck_withdraws_the_loose_ones_once_and_is_idempotent(self) -> None:
        self.grant_claim_challenge()
        summary = self._driver().run_once()["reinstatement_recheck"]
        self.assertEqual(summary["candidates"], 2)
        self.assertEqual([item["claim_version_ref"] for item in summary["withdrawn"]],
                         [self.loose["ref"]])
        self.assertEqual(summary["confirmed"], 1)
        retired = retired_claim_version_refs(self.store.connection)
        self.assertIn(self.loose["ref"], retired)
        self.assertNotIn(self.good["ref"], retired)
        reads = self.spool.reads
        again = self._driver().run_once()["reinstatement_recheck"]
        self.assertEqual(again["candidates"], 1)
        self.assertEqual(again["already_reviewed"], 1)
        self.assertEqual(again["withdrawn"], [])
        self.assertEqual(self.spool.reads, reads)
        # And the re-review never puts the withdrawn one back.
        self.assertNotIn(self.loose["ref"], {
            row["ref"] for row in self._driver().span_retirements_to_rereview()})

    def test_without_the_grant_the_recheck_only_reports(self) -> None:
        summary = self._driver().run_once()["reinstatement_recheck"]
        self.assertEqual(summary["withdrawn"], [])
        self.assertEqual([item["claim_version_ref"] for item in summary["would_withdraw"]],
                         [self.loose["ref"]])
        self.assertNotIn(self.loose["ref"], retired_claim_version_refs(self.store.connection))


class ReadPathTests(_V2Retirement):
    """Every read path that skips retired Claims takes the reinstatement back."""

    def test_the_shared_read_is_what_every_path_sees(self) -> None:
        from dalton_core.deep_insight_gate_cli import retired_claim_refs as gate_retired
        from dalton_core.deliverable_reopen import _retired_claim_refs
        from dalton_core.mission_deliverable import MissionDeliverableAuthority
        from dalton_core.mission_stage import retired_claim_refs

        claim = self.claim(statement="Automakers see demand.", source=AWS_DOC,
                           span=(0, 20))
        self.retire_v2(claim, cited_span=AWS_DOC[:20], needles=["amazon"], source=AWS_DOC)
        connection = self.store.connection
        deliverables = MissionDeliverableAuthority(self.store)
        for read in (retired_claim_refs, gate_retired, _retired_claim_refs):
            self.assertIn(claim["ref"], read(connection))
        self.assertNotIn(claim["ref"], deliverables.live_claim_version_refs())
        self.authority.reinstate(claim_version_ref=claim["ref"], actor_ref="human:lumos",
                                 rationale="r")
        for read in (retired_claim_refs, gate_retired, _retired_claim_refs):
            self.assertNotIn(claim["ref"], read(connection))
        self.assertIn(claim["ref"], deliverables.live_claim_version_refs())

    def test_the_dossier_change_key_moves_on_a_reinstatement(self) -> None:
        from dalton_core.lane_change_key import append_probe

        claim = self.claim(statement="Automakers see demand.", source=AWS_DOC,
                           span=(0, 20))
        self.retire_v2(claim, cited_span=AWS_DOC[:20], needles=["amazon"], source=AWS_DOC)
        before = append_probe(self.store.connection, "claim_retirement_reinstatements")
        self.authority.reinstate(claim_version_ref=claim["ref"], actor_ref="human:lumos",
                                 rationale="r")
        self.assertNotEqual(
            before, append_probe(self.store.connection, "claim_retirement_reinstatements"))

    def test_a_core_without_the_tables_reads_as_nothing_retired(self) -> None:
        connection = sqlite3.connect(":memory:")
        self.addCleanup(connection.close)
        self.assertEqual(retired_claim_version_refs(connection), set())
        self.assertEqual(reinstated_claim_version_refs(connection), set())
        self.assertIn("absent", retirement_state_probe(connection))


class WriterDoorTests(unittest.TestCase):
    def test_the_owner_door_is_a_human_governance_operation(self) -> None:
        from dalton_core.writer_server import (
            HUMAN_GOVERNANCE_OPERATIONS,
            OPERATION_FIELDS,
        )

        self.assertIn("reinstate_claim_retirement", HUMAN_GOVERNANCE_OPERATIONS)
        self.assertEqual(OPERATION_FIELDS["reinstate_claim_retirement"], frozenset({
            "claim_version_ref", "decision_hash", "rationale", "actor_ref"}))
        self.assertIn("withdraw_claim_reinstatement", HUMAN_GOVERNANCE_OPERATIONS)
        self.assertEqual(OPERATION_FIELDS["withdraw_claim_reinstatement"], frozenset({
            "claim_version_ref", "reinstatement_hash", "rationale", "actor_ref"}))

    def test_the_cli_is_a_dry_run_unless_told_otherwise(self) -> None:
        from dalton_core import claim_reinstatement_cli

        import contextlib
        import io

        with self.assertRaises(SystemExit), contextlib.redirect_stderr(io.StringIO()):
            claim_reinstatement_cli.main(["reinstate", "--state-dir", "/nonexistent"])
        self.assertEqual(claim_reinstatement_cli.OPERATION, "reinstate_claim_retirement")


if __name__ == "__main__":
    unittest.main()
