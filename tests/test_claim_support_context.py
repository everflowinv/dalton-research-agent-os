"""2026-09-26b: the support check reads whole sentences and the document's facts.

Post-deploy of batch 2026-09-25d, about half of the day's ``not_supported``
verdicts were statements the cited text said almost word for word: the quarter
anchored from the document's date, the company whose own call it was, "BofA
noted" on a BofA note -- all read as facts the text added -- and backfill spans
that were 1,200-character slices cutting the sentence in half.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import unittest
from types import SimpleNamespace

from dalton_core.claim_support_context import (
    cited_passage,
    document_facts,
    sentence_bounds,
    speaker_at,
)
from dalton_core.claim_support_verification import (
    CONTRACT_REF,
    build_prompt,
    item_key,
    support_item,
)
from dalton_core.document_extraction import DocumentExtractionService

TRANSCRIPT = (
    "发言人Operator： Please stand by.\n"
    "发言人Jatin Dalal： We delivered a solid second quarter with organic revenue growth at the "
    "high end of our expectations and year-over-year adjusted operating margin expansion. "
    "Nearly all our healthy sequential growth was driven by our organic business.\n"
    "发言人Maggie Nolan： Thanks. A question on margins."
)


class SentenceTests(unittest.TestCase):
    def test_a_slice_cut_mid_sentence_is_widened_to_the_whole_sentences(self) -> None:
        start = TRANSCRIPT.index("growth at the high end")
        end = TRANSCRIPT.index("adjusted operating")
        new_start, new_end = sentence_bounds(TRANSCRIPT, start, end, max_chars=2400)
        self.assertEqual(TRANSCRIPT[new_start:new_end],
                         "发言人Jatin Dalal： We delivered a solid second quarter with organic revenue "
                         "growth at the high end of our expectations and year-over-year adjusted "
                         "operating margin expansion.")

    def test_a_span_on_sentence_bounds_is_left_alone(self) -> None:
        text = "EPAM said demand improved. Other remarks followed."
        self.assertEqual(sentence_bounds(text, 0, 27, max_chars=2400), (0, 27))
        self.assertEqual(sentence_bounds(text, 27, len(text), max_chars=2400), (27, len(text)))

    def test_run_together_transcript_sentences_are_still_sentences(self) -> None:
        text = ("I think do we have the sales muscle.That's why we are risk-adjusting the pipeline "
                "itself.And we are not fully including them.So yes, the U.S.A office agrees.")
        start = text.index("risk-adjusting")
        new_start, new_end = sentence_bounds(text, start, start + 10, max_chars=2400)
        self.assertEqual(text[new_start:new_end],
                         "That's why we are risk-adjusting the pipeline itself.")

    def test_the_widening_never_passes_the_prompt_bound(self) -> None:
        text = "x" * 5000 + " end."
        start, end = sentence_bounds(text, 2000, 2100, max_chars=300)
        self.assertLessEqual(end - start, 300)
        self.assertEqual((start, end), (2000, 2100))  # no boundary within reach
        self.assertEqual(sentence_bounds(text, 10, 5, max_chars=300), (10, 5))

    def test_the_speaker_is_the_label_whose_turn_the_span_is_in(self) -> None:
        position = TRANSCRIPT.index("Nearly all")
        self.assertEqual(speaker_at(TRANSCRIPT, position), "Jatin Dalal")
        self.assertEqual(speaker_at(TRANSCRIPT, TRANSCRIPT.index("发言人Jatin")), "Jatin Dalal")
        self.assertEqual(speaker_at(TRANSCRIPT, TRANSCRIPT.index("A question")), "Maggie Nolan")
        self.assertIsNone(speaker_at("BofA TMT: the desk says so.", 10))
        passage = cited_passage(TRANSCRIPT, position, position + 20, max_chars=2400)
        self.assertEqual(passage["speaker"], "Jatin Dalal")
        self.assertTrue(passage["cited_text"].startswith("Nearly all"))


class _Spool:
    def __init__(self, objects):
        self.objects = objects

    def read_object(self, digest):
        return self.objects[digest]


class FactsTests(unittest.TestCase):
    def setUp(self) -> None:
        self.db = sqlite3.connect(":memory:")
        self.addCleanup(self.db.close)
        self.db.executescript(
            "CREATE TABLE document_provenance_records(document_ref TEXT PRIMARY KEY, broker TEXT, "
            "title TEXT, published_at TEXT);"
            "CREATE TABLE connector_source_envelopes(source_envelope_id TEXT PRIMARY KEY, record_json TEXT);"
            "CREATE TABLE observability_artifact_versions_v2(version_id TEXT PRIMARY KEY, "
            "artifact_content_hash TEXT);")
        self.db.execute("INSERT INTO document_provenance_records VALUES(?,?,?,?)",
                        ("alphaengine-doc:1", None, "Cognizant Technology Solutions Q2 2026",
                         "2026-07-29 00:00:00"))
        raw = json.dumps({"note": {"note_id": "sales-note:1", "subject": "BofA TMT: 4/27 - BIG TECH WEEK",
                                   "sent_at": "2026-04-27T11:00:00+00:00",
                                   "sender_domain": "bofa.com"}}).encode("utf-8")
        digest = hashlib.sha256(raw).hexdigest()
        self.spool = _Spool({digest: raw})
        self.db.execute("INSERT INTO connector_source_envelopes VALUES(?,?)",
                        ("source-envelope:1", json.dumps({"raw_artifact_version_ref": "artifact:1"})))
        self.db.execute("INSERT INTO observability_artifact_versions_v2 VALUES(?,?)",
                        ("artifact:1", digest))

    def test_a_transcript_is_titled_and_dated_by_its_provenance_row(self) -> None:
        facts = document_facts(self.db, document_ref="alphaengine-doc:1", period="Q2 FY2026",
                               speaker="Jatin Dalal")
        self.assertEqual(facts, {"title": "Cognizant Technology Solutions Q2 2026", "date": "2026-07-29",
                                 "period": "Q2 FY2026", "speaker": "Jatin Dalal"})

    def test_a_sales_note_is_titled_dated_and_attributed_by_its_own_header(self) -> None:
        facts = document_facts(self.db, document_ref="sales-note:1",
                               source_envelope_ref="source-envelope:1", spool=self.spool)
        self.assertEqual(facts, {"title": "BofA TMT: 4/27 - BIG TECH WEEK", "date": "2026-04-27",
                                 "house": "BofA Securities"})
        # Bytes that do not hash to the recorded artefact are no facts.
        tampered = _Spool({key: value + b" " for key, value in self.spool.objects.items()})
        self.assertEqual(document_facts(self.db, document_ref="sales-note:1",
                                        source_envelope_ref="source-envelope:1", spool=tampered), {})

    def test_the_callers_date_comes_first_and_nothing_is_guessed(self) -> None:
        facts = document_facts(self.db, document_ref="alphaengine-doc:1", document_date="2026-07-30")
        self.assertEqual(facts["date"], "2026-07-30")
        self.assertEqual(document_facts(None, document_ref="x"), {})


class PromptTests(unittest.TestCase):
    def test_the_prompt_carries_the_facts_and_says_what_anchoring_is(self) -> None:
        item = support_item(
            subject_ref="company:ticker:ctsh", subject_name="Cognizant (CTSH)",
            statement="In Q2 2026, Cognizant reported organic revenue growth at the high end of its expectations.",
            cited_text="We delivered a solid second quarter with organic revenue growth at the high end of our expectations.",
            document={"title": "Cognizant Technology Solutions Q2 2026", "date": "2026-07-29",
                      "period": "Q2 FY2026", "speaker": "Jatin Dalal"},
            producer_route_ref="route-decision:drafter")
        prompt = build_prompt([item])
        payload = json.loads(prompt[prompt.rindex("UNTRUSTED_ITEMS=") + len("UNTRUSTED_ITEMS="):])
        self.assertEqual(payload[0]["document"]["speaker"], "Jatin Dalal")
        for phrase in ("never added facts", "against document.date or the title",
                       "never by itself makes a year or quarter supported",
                       "Cognizant's Ravi Kumar said", "a third party the text quotes is not the house",
                       "condensation or summary that keeps the meaning"):
            self.assertIn(phrase, prompt)
        # The v1 wording that counted the speaker as something to prove is gone.
        self.assertNotIn("including its direction, negation, uncertainty and who said it", prompt)

    def test_the_question_is_keyed_by_contract_and_facts(self) -> None:
        self.assertEqual(CONTRACT_REF, "claim-support-verification:v3")
        base = dict(subject_ref="s", statement="a", cited_text="b")
        self.assertNotEqual(item_key(**base), item_key(**base, document={"period": "Q2"}))
        self.assertEqual(item_key(**base, document={}), item_key(**base))


class AdmissionPassageTests(unittest.TestCase):
    def test_admission_asks_about_the_whole_sentence_with_the_window_facts(self) -> None:
        db = sqlite3.connect(":memory:")
        self.addCleanup(db.close)
        db.execute("CREATE TABLE document_provenance_records(document_ref TEXT PRIMARY KEY, broker TEXT, "
                   "title TEXT, published_at TEXT)")
        db.execute("INSERT INTO document_provenance_records VALUES(?,?,?,?)",
                   ("alphaengine-doc:1", None, "Cognizant Technology Solutions Q2 2026", None))
        offset = 12000
        half = len(TRANSCRIPT) // 2
        context = {"offset": offset, "document_ref": "alphaengine-doc:1", "document_date": "2026-07-29",
                   "quotes": [{"raw_text": TRANSCRIPT[:half]}, {"raw_text": TRANSCRIPT[half:]}]}
        service = SimpleNamespace(writer=SimpleNamespace(store=SimpleNamespace(connection=db)))
        passage = DocumentExtractionService._support_passage(service, context)
        start = TRANSCRIPT.index("organic revenue")
        end = TRANSCRIPT.index(" and year-over-year")
        cited, facts = passage({"period": "Q2 FY2026", "citation": {
            "raw_text": TRANSCRIPT[start:end], "source_start": offset + start, "source_end": offset + end}})
        self.assertTrue(cited.startswith("发言人Jatin Dalal： We delivered"))
        self.assertTrue(cited.endswith("operating margin expansion."))
        self.assertEqual(facts, {"title": "Cognizant Technology Solutions Q2 2026", "date": "2026-07-29",
                                 "period": "Q2 FY2026", "speaker": "Jatin Dalal"})
        # A citation that does not sit in the window is asked about as it was cited.
        cited, _facts = passage({"period": "Q2", "citation": {"raw_text": "elsewhere", "source_start": 3,
                                                              "source_end": 12}})
        self.assertEqual(cited, "elsewhere")


if __name__ == "__main__":
    unittest.main()
