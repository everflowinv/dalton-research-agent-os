"""2026-09-24 audit: the claim review patrol read the wrong spool and the wrong scale.

* ws-7d: 2,605 qualitative Claims, every one "unreadable".  The writer handed
  the patrol its transcript spool; 536 of the 565 originals were in
  ``<state>/connector-spool`` and none in the transcript spool.  The other 29
  (and legacy's 268) are fetched web pages, whose citable original is the
  deterministic rendering of the body, so no spool object carries that hash.
* The subject-absent detector read the whole original.  A morning digest
  names every covered company somewhere, so an industry fact filed under one
  of them passed; the span-level rule catches it.
"""

from __future__ import annotations

import gzip
import hashlib
import tempfile
import unittest
from pathlib import Path

from dalton_core.claim_retirement import detect
from dalton_core.claim_review import (
    DETECTOR_SET_REF,
    ClaimReviewDriver,
    needles_from_plans,
    review_spool,
)
from dalton_core.claim_subject import (
    document_is_subjects,
    mission_subject_needles,
    name_needles,
    span_names_subject_for_admission,
    subject_absent_from_citation,
)
from tests.test_claim_retirement import EPAM, ClaimRetirementHarness

# A digest that names EPAM once, far from the span the Claim cites, and whose
# head does not name it: the whole-document rule is satisfied, the span is not.
DIGEST = (
    "Morning digest. Automakers see power generation and energy storage as the "
    "most tangible near-term opportunity for industrial suppliers. "
    + "Filler sentence about macro flows. " * 20
    + "Elsewhere, EPAM Systems was mentioned in passing."
)
SPAN = (0, 150)
EPAM_NOTE = "EPAM Systems 2Q review: management sees engineering demand improving. " + DIGEST


class ClaimSubjectRuleTests(unittest.TestCase):
    def test_name_needles_split_names_into_distinctive_words(self) -> None:
        self.assertEqual(name_needles(["Amazon.com"]), ["amazon", "amazon.com"])
        self.assertEqual(name_needles(["Meta Platforms", "META"]), ["meta", "meta platforms"])
        self.assertEqual(name_needles(["DXC Technology"]), ["dxc", "dxc technology"])
        self.assertEqual(name_needles(["埃森哲"]), ["埃森哲"])
        self.assertEqual(name_needles(["Technology", "Inc"]), [])

    def test_mission_needles_are_the_union_of_every_alias_source(self) -> None:
        universe = [{"company_ref": "company:ticker:googl", "ticker": "GOOGL"},
                    {"company_ref": "company:sec-cik:0001467373", "ticker": "ACN"}]
        feed = {"companies": {"company:ticker:googl": {"names": ["Alphabet", "GOOGL"]}}}
        search = {"companies": {"company:ticker:googl": {"search_terms": "GOOGL Google"}}}
        table = mission_subject_needles(universe, plans=[feed, search])
        # The packaged names (2026-09-24: GOOGL has some) are part of the union.
        self.assertEqual(table["company:ticker:googl"],
                         ["alphabet", "alphabet inc.", "googl", "google", "谷歌"])
        # The packaged COMPANY_NAMES answers for the legacy tickers.
        self.assertIn("accenture", table["company:sec-cik:0001467373"])

    def test_feed_plan_names_reach_the_review_needles(self) -> None:
        plans = [{"companies": {"company:ticker:googl": {"search_terms": "GOOGL"}}},
                 {"companies": {"company:ticker:googl": {"names": ["Alphabet", "GOOGL"]}}}]
        self.assertEqual(needles_from_plans(plans)["company:ticker:googl"], ["alphabet", "googl"])

    def test_span_rule_fires_only_when_span_and_statement_are_silent_off_the_subjects_own_document(self) -> None:
        needles = ["epam"]
        self.assertTrue(subject_absent_from_citation(
            span="Automakers see power generation demand.", statement="Automakers see demand.",
            needles=needles, document_is_own=False))
        self.assertFalse(subject_absent_from_citation(
            span="Automakers see power generation demand.", statement="EPAM sees demand.",
            needles=needles, document_is_own=False))
        self.assertFalse(subject_absent_from_citation(
            span="We see engineering demand improving.", statement="Management sees demand.",
            needles=needles, document_is_own=True))
        self.assertFalse(subject_absent_from_citation(
            span="anything", statement="anything", needles=[], document_is_own=False))

    def test_admission_rule_requires_the_span_itself_to_name_the_subject(self) -> None:
        self.assertFalse(span_names_subject_for_admission(
            span="CoreWeave's top three customers are most of revenue.", needles=["amazon", "amzn"],
            document_is_own=False))
        self.assertTrue(span_names_subject_for_admission(
            span="AWS (Amazon) grew backlog.", needles=["amazon", "amzn"], document_is_own=False))
        self.assertTrue(span_names_subject_for_admission(
            span="We grew backlog.", needles=["amazon"], document_is_own=True))

    def test_a_document_is_the_subjects_own_by_title_head_or_kind(self) -> None:
        self.assertTrue(document_is_subjects(title="DXC Technology Q1 2027 Earnings Call",
                                             needles=["dxc"]))
        self.assertTrue(document_is_subjects(text="EPAM Systems 2Q review: ..." + "x" * 5000,
                                             needles=["epam"]))
        self.assertFalse(document_is_subjects(text="x" * 5000 + " EPAM", needles=["epam"]))
        self.assertTrue(document_is_subjects(needles=["ibm"], issuer_document=True))

    def test_detect_reports_the_span_rule_with_its_own_rationale(self) -> None:
        hit = detect(statement="Automakers see power demand.", source_text=DIGEST, needles=["epam"],
                     cited_span=DIGEST[SPAN[0]:SPAN[1]], document_is_own=False)
        self.assertEqual(hit[0], "subject_absent_from_source")
        self.assertIn("片段", hit[1])
        # Without a span only the whole-document rule can fire, as before.
        self.assertIsNone(detect(statement="Automakers see power demand.", source_text=DIGEST,
                                 needles=["epam"]))


class SpanLevelPatrolTests(ClaimRetirementHarness):
    def test_an_industry_fact_filed_under_the_company_is_challenged_and_retired(self) -> None:
        self.grant_claim_challenge()
        wrong = self.claim(statement="Automakers see power generation as a near-term opportunity.",
                           source=DIGEST, span=SPAN)
        named = self.claim(statement="EPAM is exposed to the same industrial power build-out.",
                           source=DIGEST + " ", span=SPAN)
        own = self.claim(statement="Management sees engineering demand improving.",
                         source=EPAM_NOTE, span=(13, 70))
        summary = self.driver().run_once()
        retired = {item["claim_version_ref"] for item in summary["retired"]}
        self.assertEqual(retired, {wrong["ref"]})
        self.assertNotIn(named["ref"], retired)
        self.assertNotIn(own["ref"], retired)

    def test_a_recorded_title_naming_the_subject_exempts_its_document(self) -> None:
        self.store.connection.execute(
            "CREATE TABLE IF NOT EXISTS document_provenance_records("
            "document_ref TEXT PRIMARY KEY, title TEXT, published_at TEXT)")
        self.store.connection.execute(
            "INSERT INTO document_provenance_records VALUES(?,?,?)",
            ("alphaengine-doc:title-names-epam", "EPAM Systems Q2 2026 Earnings Call", None))
        self.grant_claim_challenge()
        held = self.claim(statement="Management sees power generation demand.", source=DIGEST,
                          span=SPAN, document_ref="alphaengine-doc:title-names-epam")
        summary = self.driver().run_once()
        self.assertEqual(summary["retired"], [])
        row = self.store.connection.execute(
            "SELECT outcome FROM claim_review_examinations WHERE claim_version_ref=?",
            (held["ref"],)).fetchone()
        self.assertEqual(row["outcome"], "clear")

    def test_the_authority_reruns_the_span_rule_itself(self) -> None:
        claim = self.claim(statement="Automakers see power generation demand.", source=DIGEST, span=SPAN)
        record = self.authority.challenge(
            claim_version_ref=claim["ref"], claim_version_hash=claim["hash"],
            reason_code="subject_absent_from_source", rationale="span", actor_ref="automation:coverage-mission")
        from dalton_core.claim_retirement import ClaimRetirementConflict
        # The document names EPAM, so without the span the detector no longer fires.
        with self.assertRaises(ClaimRetirementConflict):
            self.authority.decide(
                challenge_ref=record["id"], challenge_hash=record["content_hash"], decision="retired",
                actor_ref="automation:coverage-mission", rationale="r", subject_needles=["epam"],
                source_text=DIGEST)
        # The subject's own document: the span rule does not fire either.
        with self.assertRaises(ClaimRetirementConflict):
            self.authority.decide(
                challenge_ref=record["id"], challenge_hash=record["content_hash"], decision="retired",
                actor_ref="automation:coverage-mission", rationale="r", subject_needles=["epam"],
                source_text=DIGEST, cited_span=DIGEST[:150], document_is_own=True)
        decision = self.authority.decide(
            challenge_ref=record["id"], challenge_hash=record["content_hash"], decision="retired",
            actor_ref="automation:coverage-mission", rationale="r", subject_needles=["epam"],
            source_text=DIGEST, cited_span=DIGEST[:150], document_is_own=False)
        self.assertEqual(decision["decision"], "retired")


class DetectorBumpTests(ClaimRetirementHarness):
    def _mark(self, claim: dict[str, str], outcome: str, detector: str) -> None:
        self.driver()  # creates the examination schema
        self.store.connection.create_function("dalton_claim_review_authorized", 0, lambda: 1)
        self.store.connection.execute(
            "INSERT INTO claim_review_examinations(claim_version_ref,claim_version_hash,"
            "source_content_hash,detector_ref,outcome,attempts,examined_at) VALUES(?,?,?,?,?,?,?)",
            (claim["ref"], claim["hash"], claim["source_sha256"], detector, outcome, 3,
             "2026-09-20T00:00:00+00:00"))
        self.store.connection.commit()

    def test_an_unreadable_verdict_of_an_older_detector_set_is_examined_again_not_deferred(self) -> None:
        claim = self.claim(statement="Management sees stronger demand.",
                           source="Operator: welcome to the EPAM Systems call.")
        self._mark(claim, "unreadable", "claim-detectors:boilerplate+subject-absent:v1")
        driver = self.driver()
        first = driver.run_once(unreadable_retries=0)
        self.assertEqual((first["scanned"], first["deferred"], first["examined"]), (1, 0, 1))
        row = self.store.connection.execute(
            "SELECT outcome, detector_ref FROM claim_review_examinations WHERE claim_version_ref=?",
            (claim["ref"],)).fetchone()
        # The row now says which detector set examined it, so it settles.
        self.assertEqual((row["outcome"], row["detector_ref"]), ("clear", DETECTOR_SET_REF))
        second = driver.run_once(unreadable_retries=0)
        self.assertEqual((second["scanned"], second["already_examined"]), (0, 1))

    def test_a_clear_verdict_of_an_older_detector_set_is_examined_once_more(self) -> None:
        claim = self.claim(statement="Automakers see power demand.", source=DIGEST, span=SPAN)
        self._mark(claim, "clear", "claim-detectors:boilerplate+subject-absent:v1")
        self.grant_claim_challenge()
        summary = self.driver().run_once()
        self.assertEqual([item["claim_version_ref"] for item in summary["retired"]], [claim["ref"]])


class SpoolRootTests(ClaimRetirementHarness):
    def setUp(self) -> None:
        super().setUp()
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name)

    def _put(self, root: Path, body: bytes, *, archived: bool = False) -> str:
        digest = hashlib.sha256(body).hexdigest()
        shard = root / "objects" / digest[:2]
        shard.mkdir(parents=True, exist_ok=True)
        if archived:
            (shard / f"{digest}.gz").write_bytes(gzip.compress(body))
        else:
            (shard / digest).write_bytes(body)
        return digest

    def test_originals_in_the_connector_spool_and_gz_archives_are_read(self) -> None:
        # The live layout: <state>/connector-spool/connector-spool/objects and
        # <state>/transcript-spool/connector-spool/objects.
        note = b"EPAM Systems note: engineering demand improving."
        archived = b"EPAM Systems wiki page: delivery centres."
        in_connector = self._put(self.state / "connector-spool" / "connector-spool", note)
        in_raw = self._put(self.state / "raw-spool" / "connector-spool", archived, archived=True)
        (self.state / "transcript-spool" / "connector-spool" / "objects").mkdir(parents=True)
        spool = review_spool(self.state)
        self.assertEqual(spool.read_object(in_connector), note)
        self.assertEqual(spool.read_object(in_raw), archived)
        driver = ClaimReviewDriver(store=self.store, missions=self.missions, challenges=self.authority,
                                   spool=spool, needles={EPAM: ["epam"]})
        self.assertEqual(driver.source_text(in_connector), note.decode())
        self.assertIsNone(driver.source_text("0" * 64))

    def test_the_transcript_spool_alone_is_what_made_every_feed_claim_unreadable(self) -> None:
        note = "EPAM Systems note: engineering demand improving."
        self.claim(statement="Management sees demand.", source=note, store_source=False)
        digest = hashlib.sha256(note.encode()).hexdigest()
        self._put(self.state / "connector-spool" / "connector-spool", note.encode())
        transcript_only = ClaimReviewDriver(
            store=self.store, missions=self.missions, challenges=self.authority,
            spool=self.spool, needles={EPAM: ["epam"]})
        self.assertIsNone(transcript_only.source_text(digest))
        fixed = ClaimReviewDriver(
            store=self.store, missions=self.missions, challenges=self.authority,
            spool=review_spool(self.state, primary=self.spool), needles={EPAM: ["epam"]})
        summary = fixed.run_once()
        self.assertEqual((summary["unreadable"], summary["examined"]), (0, 1))

    def test_the_writer_builds_the_patrol_over_every_spool_root_and_the_feed_names(self) -> None:
        from types import SimpleNamespace

        from dalton_core.writer_server import WriterServer

        note = b"Alphabet note: cloud backlog grew."
        digest = self._put(self.state / "connector-spool" / "connector-spool", note)
        plan_path = self.state / "feed-plan.json"
        feed = SimpleNamespace(feed_plan_path=str(plan_path), spool_dir=None)
        launchers = {"sales_notes_feed_launcher": feed}
        fake = SimpleNamespace(
            store=self.store, coverage_mission=self.missions,
            claim_retirement_challenges=self.authority, _transcript_spool=self.spool,
            _lane_launchers=launchers, lane_launcher=launchers.get, state_dir=self.state,
            _source_discovery=None, _web_source_discovery=None,
            _sec_filings_source_discovery=None,
        )
        names = {"company:ticker:googl": ["alphabet", "googl"]}
        from unittest.mock import patch
        with patch("dalton_core.mission_feed_lane.load_feed_discovery_plan",
                   return_value={"companies": {"company:ticker:googl": {"names": ["Alphabet", "GOOGL"]}}}):
            driver = WriterServer._claim_review_driver(fake)
        self.assertEqual(driver.source_text(digest), note.decode())
        self.assertEqual(driver.needles["company:ticker:googl"], names["company:ticker:googl"])

    def test_a_fetched_page_is_read_through_its_verified_rendering(self) -> None:
        from dalton_core.public_web_extraction_source import render_public_web_text

        html = (b"<html><body><h1>Lighting market</h1><p>Automakers see power generation as the "
                b"near-term opportunity.</p>" + b"<p>Macro filler paragraph.</p>" * 30
                + b"<p>EPAM was not the subject.</p></body></html>")
        body = self._put(self.state / "connector-spool" / "connector-spool", html)
        rendered = render_public_web_text(html, raw_media_type="text/html; charset=utf-8")["text"]
        document_ref = f"public-web-document:url-sha256:{'a' * 64}:body-sha256:{body}"
        self.grant_claim_challenge()
        claim = self.claim(statement="Automakers see power generation demand.", source=rendered,
                           store_source=False, document_ref=document_ref, span=(0, 60))
        driver = ClaimReviewDriver(
            store=self.store, missions=self.missions, challenges=self.authority,
            spool=review_spool(self.state), needles={EPAM: ["epam"]})
        self.assertEqual(driver.run_once(max_renders=0)["deferred"], 1)
        summary = driver.run_once()
        self.assertEqual(summary["unreadable"], 0)
        self.assertEqual([item["claim_version_ref"] for item in summary["retired"]], [claim["ref"]])

    def test_a_rendering_that_does_not_hash_to_the_recorded_original_is_unreadable(self) -> None:
        html = b"<html><body><p>EPAM Systems page.</p></body></html>"
        body = self._put(self.state / "connector-spool" / "connector-spool", html)
        document_ref = f"public-web-document:url-sha256:{'b' * 64}:body-sha256:{body}"
        self.claim(statement="Management sees demand.", source="a different rendering entirely",
                   store_source=False, document_ref=document_ref)
        driver = ClaimReviewDriver(
            store=self.store, missions=self.missions, challenges=self.authority,
            spool=review_spool(self.state), needles={EPAM: ["epam"]})
        self.assertEqual(driver.run_once()["unreadable"], 1)


if __name__ == "__main__":
    unittest.main()
