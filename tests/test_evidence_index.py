"""Batch A (证据底座): full-text evidence index, search, passage citations, arrivals.

Real-shaped fixtures for every acquired-document kind -- an AlphaEngine
transcript and broker reports, a sales note, a company-wiki page, a Guidepoint
excerpt, a public web page, an SEC 8-K with its Exhibit 99.1 and an inline-XBRL
10-K -- laid out on disk exactly as the acquisition children write them.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from dalton_core import evidence_index as ei

ACN = "company:sec-cik:0001467373"
EPAM = "company:sec-cik:0001352010"
OTHER = "company:sec-cik:0000000009"
MISSION = {"id": "coverage-mission-version:test:1", "mission_ref": "coverage-mission:test", "version": 1,
           "industry_ref": "industry:us-it-services",
           "universe": [{"company_ref": ACN, "ticker": "ACN", "name": "Accenture"},
                        {"company_ref": EPAM, "ticker": "EPAM", "name": "EPAM Systems"}]}
ACCESSION_8K = "0001467373-26-000037"
ACCESSION_10K = "0001467373-25-000200"

FILLER = ("Clients continue to prioritise large transformations and managed services. " * 6).strip()

TRANSCRIPT = (
    "发言人Operator： Good day, and welcome to the Accenture fourth quarter 2026 conference call.\n"
    f"发言人Julie Sweet： Thank you. {FILLER} New bookings were $22.2 billion for the quarter, "
    f"a 4% increase in US dollars and 5% in local currency. {FILLER} {FILLER}\n"
    f"发言人Angie Park： Let me turn to the business outlook. {FILLER} For the full fiscal 2027, we expect "
    f"revenue to be in the range of 3% to 6% growth in local currency. Our guidance assumes bookings "
    f"momentum continues. {FILLER} {FILLER}\n"
    f"发言人Analyst： Can you talk about bookings? {FILLER}\n"
)
GS_REPORT = (
    "Accenture Plc (ACN): 4Q Preview\n\n"
    "Key Debates Into the Print\n\n"
    "We expect tepid FY27 guidance given limited demand visibility. Bookings growth should remain "
    "the key swing factor for FY27 guidance, and we model bookings of $21.0 billion.\n\n"
    f"{FILLER}\n\n{FILLER}\n"
)
OLD_REPORT = GS_REPORT.replace("4Q Preview", "Initiation")
SALES_NOTE = (
    "[cid:image001.png@01DD518B.999C8A60]\n\n"
    "BofA TMT: 10/1 - Accenture, MU, Software\n"
    "________________________________\n"
    "MACRO: Nasdaq 0.2%. Accenture +6% on bookings beat; IT services basket ripping.\n"
)
WIKI = "EPAM Systems 公司概况：存储需求上升带动工程服务。EPAM Systems revenue mix shifted toward AI work.\n"
GUIDEPOINT = ("Q: How is pricing in IT services moving?\nA: Pricing pressure is real; Accenture and "
              "EPAM Systems both discount large deals.\n")
WEB_HTML = ("<html><head><title>Accenture beats on bookings</title></head><body>"
            "<p>Accenture reported bookings of $22.2 billion and raised its FY27 outlook.</p>"
            "<script>var x = 'hidden';</script></body></html>")
EIGHT_K_HTML = ("<html><body><p>ITEM 2.02 Results of Operations and Financial Condition</p>"
                "<p>On October 1, 2026, Accenture plc issued a press release announcing results.</p>"
                "</body></html>")
EXHIBIT_HTML = ("<html><body><p>Accenture Reports Fourth-Quarter and Full-Year Fiscal 2026 Results</p>"
                "<p>New bookings of $22.2 billion, an increase of 4% in U.S. dollars.</p>"
                "<p>Fiscal Year 2027 Business Outlook: revenue growth of 3% to 6% in local currency; "
                "GAAP diluted EPS of $14.39 to $14.81.</p></body></html>")
TEN_K_HTML = ("<html><body><div style='display:none'><ix:header><ix:hidden>"
              "<ix:nonNumeric name='dei:EntityCentralIndexKey' contextRef='c1'>0001467373</ix:nonNumeric>"
              "</ix:hidden><ix:resources>context c1 2024-09-01 2025-08-31 iso4217:USD</ix:resources>"
              "</ix:header></div>"
              "<p>PART II</p><p>Item 7. Management's Discussion and Analysis</p>"
              "<p>Revenues were $<ix:nonFraction name='us-gaap:Revenues' contextRef='c1' unitRef='usd' "
              "scale='6' decimals='-6'>69,673</ix:nonFraction> million for fiscal 2025.</p></body></html>")

CORE_SCHEMA = """
CREATE TABLE coverage_mission_versions(mission_version_id TEXT PRIMARY KEY, mission_ref TEXT,
    version_number INTEGER, record_json TEXT);
CREATE TABLE coverage_mission_pointer(mission_ref TEXT PRIMARY KEY, mission_version_id TEXT, updated_at TEXT);
CREATE TABLE coverage_mission_discovered_documents(record_id TEXT PRIMARY KEY, company_ref TEXT, source_ref TEXT,
    document_ref TEXT, ticket_ref TEXT, discovery_ref TEXT, status TEXT, created_at TEXT);
CREATE TABLE document_provenance_records(document_ref TEXT PRIMARY KEY, source_ref TEXT, provenance_tier TEXT,
    broker TEXT, title TEXT, published_at TEXT, record_json TEXT);
CREATE TABLE earnings_release_reads(read_id TEXT PRIMARY KEY, company_ref TEXT, accession TEXT, status TEXT,
    period TEXT, exhibit_url TEXT);
CREATE TABLE coverage_mission_statement_filings(ingest_id TEXT PRIMARY KEY, company_ref TEXT, cik TEXT,
    entity_name TEXT, accession TEXT, form TEXT, filed TEXT, report_date TEXT, recorded_at TEXT);
"""


def _put(state: Path, data: bytes) -> str:
    digest = hashlib.sha256(data).hexdigest()
    shard = state / "connector-spool" / "objects" / digest[:2]
    shard.mkdir(parents=True, exist_ok=True)
    (shard / digest).write_bytes(data)
    return digest


def _write(path: Path, value: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value), encoding="utf-8")


class CorpusBuilder:
    """Writes acquisitions the way the children do, and the Core rows that describe them."""

    def __init__(self, state: Path) -> None:
        self.state = state
        self.db = sqlite3.connect(state / "core.sqlite")
        self.db.executescript(CORE_SCHEMA)
        self.db.execute("INSERT INTO coverage_mission_versions VALUES (?,?,?,?)",
                        (MISSION["id"], MISSION["mission_ref"], 1, json.dumps(MISSION)))
        self.db.execute("INSERT INTO coverage_mission_pointer VALUES (?,?,?)",
                        (MISSION["mission_ref"], MISSION["id"], "2026-10-01T00:00:00Z"))
        self.count = 0

    def _discovered(self, company: str, source: str, document_ref: str, ticket: str | None,
                    created: str = "2026-10-01T00:00:00+00:00") -> None:
        self.count += 1
        self.db.execute("INSERT INTO coverage_mission_discovered_documents VALUES (?,?,?,?,?,?,?,?)",
                        (f"d{self.count}", company, source, document_ref, ticket, None, "acquired", created))

    def alphaengine(self, ticket: str, doc_id: str, text: str, *, spec: str, title: str, broker: str | None,
                    published: str, named: list[str], company: str = ACN,
                    created: str = "2026-10-01T12:00:00+00:00") -> str:
        digest = _put(self.state, text.encode("utf-8"))
        document_ref = f"alphaengine-doc:{doc_id}"
        _write(self.state / "acquisitions" / ticket / "manifest.json", {
            "status": "complete", "document_ref": document_ref, "content_chars": len(text),
            "assembled_object": {"content_hash": digest, "size_bytes": len(text.encode())},
            "declared_content_sha256": digest, "created_at": created})
        tier = {"earnings-call-transcripts": "management", "sell-side-reports": "sell_side"}[spec]
        record = {"document_ref": document_ref, "spec_ref": spec, "provenance_tier": tier, "broker": broker,
                  "title": title, "published_at": published, "named_companies": named}
        self.db.execute("INSERT INTO document_provenance_records VALUES (?,?,?,?,?,?,?)",
                        (document_ref, "source:alphaengine", tier, broker, title, published, json.dumps(record)))
        self._discovered(company, "source:alphaengine", document_ref, f"alphaengine-acquisition:{ticket}")
        return document_ref

    def feed(self, feed: str, ticket: str, document_ref: str, text: str, *, source: str, doc_date: str,
             tickers: list[str], broker: str | None = None, company: str | None = ACN) -> str:
        digest = _put(self.state, text.encode("utf-8"))
        _write(self.state / f"feed-acquisitions-{feed}" / ticket / "manifest.json", {
            "status": "complete", "document_ref": document_ref, "source_ref": source,
            "assembled_object": {"content_hash": digest}, "declared_content_sha256": digest,
            "doc_date": doc_date, "subject_tickers": tickers, "created_at": f"{doc_date}T14:00:00+00:00"})
        if broker:
            record = {"document_ref": document_ref, "broker": broker, "named_companies": [],
                      "provenance_tier": "sales_note"}
            self.db.execute("INSERT INTO document_provenance_records VALUES (?,?,?,?,?,?,?)",
                            (document_ref, source, "sales_note", broker, None, None, json.dumps(record)))
        if company:
            self._discovered(company, source, document_ref, f"{feed}-run:{ticket}")
        return document_ref

    def guidepoint(self, ticket: str, text: str) -> str:
        digest = _put(self.state, text.encode("utf-8"))
        document_ref = "guidepoint-excerpt:sha256:" + "9" * 64
        _write(self.state / "guidepoint-acquire-runs" / ticket / "manifest.json", {
            "status": "complete", "document_ref": document_ref, "excerpt_object": {"content_hash": digest},
            "declared_content_sha256": digest, "excerpt_date": "2026-05-20",
            "respondent": "Former VP at a Global IT Services Firm",
            "source_attribution_markdown": "Source: Guidepoint Transcript — [IT Services Pricing Pressure]"
                                           "(https://platform.guidepoint.com/transcript?id=1)",
            "reference_url": "https://platform.guidepoint.com/transcript?id=1",
            "created_at": "2026-09-15T17:32:55+00:00"})
        self._discovered(EPAM, "source:guidepoint", document_ref, None)
        return document_ref

    def fetch(self, ticket: str, html: str, url: str, *, exhibit: bool = False, mission_ref: str | None = None,
              company: str = ACN) -> str:
        body = html.encode("utf-8")
        digest = _put(self.state, body)
        host = url.split("/")[2]
        _write(self.state / "fetches" / ticket / ("exhibit-manifest.json" if exhibit else "manifest.json"), {
            "body_sha256": digest, "body_bytes": len(body), "canonical_url": url, "final_url": url,
            "host": host, "raw_media_type": "text/html; charset=utf-8",
            "document_ref": f"public-web-document:url-sha256:{'1' * 64}:body-sha256:{digest}",
            "created_at": "2026-10-01T20:00:00+00:00"})
        if mission_ref and not exhibit:
            self._discovered(company, "source:sec-edgar" if "sec.gov" in host else "source:web-search",
                             mission_ref, f"public-web-fetch:{ticket}")
        return digest

    def build(self) -> dict[str, str]:
        refs = {}
        refs["transcript"] = self.alphaengine("t1", "130000114289274", TRANSCRIPT,
                                              spec="earnings-call-transcripts", title="Accenture Q4 2026",
                                              broker=None, published="2026-10-01 00:00:00",
                                              named=["Accenture PLC"])
        refs["gs"] = self.alphaengine("t2", "320000610308911", GS_REPORT, spec="sell-side-reports",
                                      title="Accenture Plc (ACN): 4Q Preview: Expect tepid FY27 guidance",
                                      broker="Goldman Sachs", published="2026-09-11 08:00:00",
                                      named=["Accenture PLC"])
        refs["old"] = self.alphaengine("t3", "320000600000001", OLD_REPORT, spec="sell-side-reports",
                                       title="Accenture Plc (ACN): Initiation: Expect tepid FY27 guidance",
                                       broker="Goldman Sachs", published="2025-03-01 08:00:00",
                                       named=["Accenture PLC"], created="2025-03-02T00:00:00+00:00")
        refs["sales"] = self.feed("sales-notes", "s1", "sales-note:1a0f7c7aed89acf4", SALES_NOTE,
                                  source="source:sales-notes", doc_date="2026-10-01", tickers=[],
                                  broker="BofA Securities")
        refs["wiki"] = self.feed("company-wiki", "w1", "company-wiki-doc:sha256:" + "8" * 64, WIKI,
                                 source="source:company-wiki", doc_date="2026-08-23", tickers=["EPAM"],
                                 company=None)
        refs["guidepoint"] = self.guidepoint("g1", GUIDEPOINT)
        refs["web"] = "web-search:result:accenture-bookings"
        self.fetch("f1", WEB_HTML, "https://news.example.com/accenture-bookings", mission_ref=refs["web"])
        base = "https://www.sec.gov/Archives/edgar/data/1467373/000146737326000037/"
        refs["8k"] = f"sec:filing:{ACCESSION_8K}"
        self.fetch("f2", EIGHT_K_HTML, base + "acn-20261001.htm", mission_ref=refs["8k"])
        self.fetch("f2", EXHIBIT_HTML, base + "q4fy26earnings8-kexhibit.htm", exhibit=True)
        refs["ex99"] = f"sec:exhibit:{ACCESSION_8K}:q4fy26earnings8-kexhibit.htm"
        refs["10k"] = f"sec:filing:{ACCESSION_10K}"
        self.fetch("f3", TEN_K_HTML,
                   "https://www.sec.gov/Archives/edgar/data/1467373/000146737325000200/acn-20250831.htm",
                   mission_ref=refs["10k"])
        self.db.execute("INSERT INTO earnings_release_reads VALUES (?,?,?,?,?,?)",
                        ("r1", ACN, ACCESSION_8K, "parsed", "FY2026-Q4", base + "q4fy26earnings8-kexhibit.htm"))
        for ingest, accession, form, filed, report in (("i1", ACCESSION_8K, "8-K", "2026-10-01", None),
                                                       ("i2", ACCESSION_10K, "10-K", "2025-10-10", "2025-08-31")):
            self.db.execute("INSERT INTO coverage_mission_statement_filings VALUES (?,?,?,?,?,?,?,?,?)",
                            (ingest, ACN, "0001467373", "Accenture plc", accession, form, filed, report,
                             "2026-10-02T00:00:00Z"))
        self.db.commit()
        return refs

    def close(self) -> None:
        self.db.close()


class IndexFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name)
        builder = CorpusBuilder(self.state)
        self.refs = builder.build()
        builder.close()
        self.summary = ei.index_tick(self.state, max_documents=100, max_seconds=60,
                                     now="2026-10-02T00:00:00.000000+00:00")

    def doc(self, document_ref: str) -> sqlite3.Row:
        connection = ei.open_index_readonly(self.state)
        self.addCleanup(connection.close)
        row = connection.execute("SELECT * FROM evidence_documents WHERE document_ref=?",
                                 (document_ref,)).fetchone()
        self.assertIsNotNone(row, document_ref)
        return row


class IndexingTests(IndexFixture):
    def test_every_kind_is_indexed_with_its_metadata(self) -> None:
        self.assertEqual(self.summary["indexed"], 10, self.summary)
        self.assertEqual((self.summary["skipped"], self.summary["failed"]), (0, 0))
        expected = {
            "transcript": ("transcript", "Accenture Q4 2026", None, "2026-10-01", "FY2026-Q4"),
            "gs": ("broker_report", "Accenture Plc (ACN): 4Q Preview: Expect tepid FY27 guidance",
                   "Goldman Sachs", "2026-09-11", "FY2027"),
            "sales": ("sales_note", "BofA TMT: 10/1 - Accenture, MU, Software", "BofA Securities",
                      "2026-10-01", None),
            "wiki": ("company_wiki", WIKI.splitlines()[0][:200], None, "2026-08-23", None),
            "guidepoint": ("expert_call", "IT Services Pricing Pressure", "Guidepoint", "2026-05-20", None),
            "web": ("web_page", "Accenture beats on bookings", "news.example.com", "2026-10-01", None),
            "8k": ("sec_8k", "Accenture plc 8-K 2026-10-01", "Accenture plc", "2026-10-01", None),
            "ex99": ("sec_exhibit", "Accenture plc 8-K Exhibit 99.1 2026-10-01", "Accenture plc",
                     "2026-10-01", "FY2026-Q4"),
            "10k": ("sec_10k", "Accenture plc 10-K 2025-10-10", "Accenture plc", "2025-10-10", "PE2025-08-31"),
        }
        for key, (kind, title, house, day, period) in expected.items():
            row = self.doc(self.refs[key])
            self.assertEqual((row["kind"], row["title"], row["house"], row["document_date"], row["period"]),
                             (kind, title, house, day, period), key)

    def test_passages_are_exact_spans_of_the_document_with_hashes(self) -> None:
        passages = ei.passages_for_document(self.state, self.refs["transcript"])
        self.assertGreater(len(passages), 1)
        for item in passages:
            self.assertEqual(item["text"], TRANSCRIPT[item["start"]:item["end"]])
            self.assertEqual(item["content_sha256"], hashlib.sha256(item["text"].encode()).hexdigest())
            self.assertLessEqual(len(item["text"]), ei.CHUNK_CHARS + 400)
            self.assertIn(ACN, item["company_refs"])
        # Overlap: each passage starts before the previous one ends.
        for before, after in zip(passages, passages[1:]):
            self.assertLess(after["start"], before["end"])
        speakers = {speaker for item in passages for speaker in item["speakers"]}
        self.assertTrue({"Julie Sweet", "Angie Park"} <= speakers, speakers)
        self.assertTrue(any((item["heading"] or "").startswith("发言人") for item in passages))

    def test_inline_xbrl_values_render_and_the_hidden_header_does_not(self) -> None:
        text = " ".join(item["text"] for item in ei.passages_for_document(self.state, self.refs["10k"]))
        self.assertIn("69,673", text)
        self.assertNotIn("iso4217", text)
        headings = [item["heading"] for item in ei.passages_for_document(self.state, self.refs["10k"])]
        self.assertTrue(any((heading or "").startswith(("PART II", "Item 7.")) for heading in headings), headings)
        self.assertIn("strip-ix-header", self.doc(self.refs["10k"])["renderer"])
        web = " ".join(item["text"] for item in ei.passages_for_document(self.state, self.refs["web"]))
        self.assertNotIn("hidden", web)

    def test_company_refs_filed_listed_and_named_in_text(self) -> None:
        wiki = ei.passages_for_document(self.state, self.refs["wiki"])
        self.assertEqual(wiki[0]["filed_company_refs"], [EPAM])  # from the wiki's own subject tickers
        guide = ei.passages_for_document(self.state, self.refs["guidepoint"])[0]
        self.assertEqual(set(guide["company_refs"]), {EPAM, ACN})  # filed EPAM; text names Accenture too
        self.assertEqual(guide["speakers"], ["Former VP at a Global IT Services Firm"])

    def test_chunking_covers_every_character_and_respects_bounds(self) -> None:
        text = ("Sentence number one is here. " * 300) + "\n\n" + ("另一个段落。" * 400)
        spans = ei.chunk_spans(text)
        covered = set()
        for start, end in spans:
            self.assertFalse(text[start].isspace() or text[end - 1].isspace())
            covered.update(range(start, end))
        self.assertTrue(all(i in covered for i, ch in enumerate(text) if not ch.isspace()))
        self.assertTrue(all(end - start <= ei.CHUNK_CHARS + 250 for start, end in spans))
        self.assertEqual(ei.chunk_spans(""), [])
        self.assertEqual(ei.chunk_spans("short"), [(0, 5)])


class IncrementalTests(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name)
        self.builder = CorpusBuilder(self.state)
        self.addCleanup(self.builder.close)
        self.refs = self.builder.build()

    def test_bounded_resumable_backfill_then_idle_then_new_arrival(self) -> None:
        first = ei.index_tick(self.state, max_documents=3, max_seconds=60)
        self.assertEqual(first["indexed"], 3)
        self.assertEqual(first["remaining"], 7)
        second = ei.index_tick(self.state, max_documents=100, max_seconds=60)
        self.assertEqual((second["indexed"], second["remaining"]), (7, 0))
        idle = ei.index_tick(self.state, max_documents=100, max_seconds=60)
        self.assertEqual(idle["indexed"] + idle["skipped"] + idle["failed"], 0)
        status = ei.index_status(self.state)
        self.assertEqual(status["documents"], 10)
        self.assertEqual(status["ledger"], {"indexed": 10})
        # A new acquisition is picked up by the next tick -- and is not backfill.
        self.builder.alphaengine("t9", "320000610399999", GS_REPORT + "\nA new paragraph.",
                                 spec="sell-side-reports", title="Accenture: Post-print",
                                 broker="Morgan Stanley", published="2026-10-02 08:00:00",
                                 named=["Accenture PLC"], created="2099-01-01T00:00:00+00:00")
        self.builder.db.commit()
        third = ei.index_tick(self.state)
        self.assertEqual(third["indexed"], 1)
        fresh = ei.material_arrivals(self.state, company_ref=ACN)
        self.assertEqual([row["document_ref"] for row in fresh], ["alphaengine-doc:320000610399999"])
        self.assertEqual(fresh[0]["backfill"], 0)

    def test_a_tampered_spool_object_is_skipped_with_its_reason(self) -> None:
        manifest = json.loads((self.state / "acquisitions" / "t2" / "manifest.json").read_text())
        digest = manifest["assembled_object"]["content_hash"]
        (self.state / "connector-spool" / "objects" / digest[:2] / digest).write_bytes(b"altered")
        ei.index_tick(self.state, max_documents=100, max_seconds=60)
        connection = ei.open_index_readonly(self.state)
        self.addCleanup(connection.close)
        row = connection.execute("SELECT status, reason FROM evidence_index_ledger WHERE source_key=?",
                                 ("alphaengine:t2",)).fetchone()
        self.assertEqual(row["status"], "skipped")
        self.assertIn("指纹", row["reason"])

    def test_the_acquisition_hook_indexes_one_manifest_now(self) -> None:
        out = ei.index_acquired_manifest(self.state, self.state / "acquisitions" / "t1" / "manifest.json")
        self.assertEqual(out["indexed"], 1)
        self.assertEqual(len(ei.passages_for_document(self.state, self.refs["transcript"])) > 0, True)
        self.assertEqual(ei.index_acquired_manifest(self.state, self.state / "elsewhere" / "x.json")["status"],
                         "ignored")


class SearchTests(IndexFixture):
    def test_bookings_fy27_guidance_ranks_recent_primary_sources_first(self) -> None:
        found = ei.evidence_search(self.state, "bookings FY27 guidance", company_ref=ACN, limit=20,
                                   date_to="2026-10-09")
        self.assertEqual(found["status"], "ok")
        self.assertEqual(found["terms"], ["bookings", "FY27", "guidance"])
        order = [item["document_ref"] for item in found["results"]]
        self.assertIn(order[0], {self.refs["transcript"], self.refs["gs"], self.refs["ex99"]})
        # Same words, same house, eighteen months older: ranked below the preview.
        self.assertLess(order.index(self.refs["gs"]), order.index(self.refs["old"]))
        top = found["results"][0]
        self.assertIn("【", top["snippet"])
        self.assertTrue(top["highlights"])
        self.assertEqual(set(top["score_parts"]), {"bm25", "relevance", "coverage", "recency", "kind_weight"})
        # "fiscal 2027" in the transcript answers FY27.
        transcript_hits = [item for item in found["results"] if item["document_ref"] == self.refs["transcript"]]
        self.assertTrue(any("fiscal 2027" in item["text"] for item in transcript_hits))

    def test_filters_kind_date_company(self) -> None:
        only = ei.evidence_search(self.state, "bookings", kinds=["sales_note"])
        self.assertEqual({item["kind"] for item in only["results"]}, {"sales_note"})
        dated = ei.evidence_search(self.state, "guidance", date_from="2026-01-01")
        self.assertNotIn(self.refs["old"], [item["document_ref"] for item in dated["results"]])
        epam = ei.evidence_search(self.state, "pricing", company_ref=EPAM)
        self.assertEqual([item["document_ref"] for item in epam["results"]], [self.refs["guidepoint"]])
        self.assertEqual(ei.evidence_search(self.state, "bookings", company_ref=OTHER)["status"], "empty")
        with self.assertRaises(ei.EvidenceIndexError):
            ei.evidence_search(self.state, "x", kinds=["nonsense"])
        with self.assertRaises(ei.EvidenceIndexError):
            ei.evidence_search(self.state, "   ")

    def test_cjk_two_character_query_matches(self) -> None:
        found = ei.evidence_search(self.state, "存储")
        self.assertEqual([item["document_ref"] for item in found["results"]], [self.refs["wiki"]])
        self.assertIn("【存储】", found["results"][0]["snippet"])

    def test_no_index_is_an_honest_status(self) -> None:
        with tempfile.TemporaryDirectory() as blank:
            self.assertEqual(ei.evidence_search(blank, "bookings")["status"], "no_index")
            self.assertEqual(ei.passages_for_document(blank, self.refs["gs"]), [])
            self.assertIsNone(ei.passage(blank, f"doc-passage:{self.refs['gs']}:0-10:{'a' * 12}"))


class PassageRefTests(IndexFixture):
    def test_round_trip_full_passage_and_sub_span(self) -> None:
        item = ei.evidence_search(self.state, "New bookings were", company_ref=ACN)["results"][0]
        again = ei.passage(self.state, item["passage_ref"])
        self.assertEqual(again["text"], item["text"])
        quote = "New bookings were $22.2 billion"
        start = TRANSCRIPT.index(quote)
        ref = ei.make_passage_ref(self.refs["transcript"], start, start + len(quote), quote)
        self.assertRegex(ref, r"^doc-passage:alphaengine-doc:130000114289274:\d+-\d+:[0-9a-f]{12}$")
        cited = ei.passage(self.state, ref)
        self.assertEqual(cited["text"], quote)
        self.assertEqual((cited["kind"], cited["date"], cited["period"]), ("transcript", "2026-10-01", "FY2026-Q4"))
        self.assertEqual(ei.parse_passage_ref(ref)["document_ref"], self.refs["transcript"])
        self.assertEqual(ei.verify_against_source(self.state, ref)["status"], "verified")

    def test_tampering_with_the_ref_or_the_index_is_detected(self) -> None:
        quote = "New bookings were $22.2 billion"
        start = TRANSCRIPT.index(quote)
        ref = ei.make_passage_ref(self.refs["transcript"], start, start + len(quote), quote)
        forged = ref[:-12] + ("0" * 12 if not ref.endswith("0" * 12) else "1" * 12)
        with self.assertRaises(ei.PassageTampered):
            ei.passage(self.state, forged)
        shifted = ei.make_passage_ref(self.refs["transcript"], start + 1, start + len(quote) + 1, quote)
        with self.assertRaises(ei.PassageTampered):
            ei.passage(self.state, shifted)
        with self.assertRaises(ei.PassageRefInvalid):
            ei.passage(self.state, "claim-version:abc")
        self.assertIsNone(ei.passage(self.state, f"doc-passage:alphaengine-doc:999:0-5:{'a' * 12}"))
        self.assertIsNone(ei.passage_resolver(self.state)(forged))
        # Altering the stored passage text is caught by its stored hash.
        connection = sqlite3.connect(ei.index_path(self.state))
        connection.execute("UPDATE evidence_passages SET text=replace(text, '22.2', '99.9')")
        connection.commit()
        connection.close()
        with self.assertRaises(ei.PassageTampered):
            ei.passage(self.state, ref)

    def test_render_passage_block_numbers_and_maps_tags(self) -> None:
        results = ei.evidence_search(self.state, "bookings FY27 guidance", company_ref=ACN, limit=3)["results"]
        block = ei.render_passage_block(results, start=12, company_labels={ACN: "Accenture ACN"})
        self.assertEqual(list(block["tags"]), ["P12", "P13", "P14"])
        self.assertEqual(block["tags"]["P12"], results[0]["passage_ref"])
        first = block["text"].splitlines()[0]
        self.assertTrue(first.startswith("[P12] · "))
        self.assertIn(results[0]["kind_label"], first)
        self.assertIn(results[0]["date"], first)
        self.assertIn("涉及：Accenture ACN", first)
        self.assertEqual(block["next_number"], 15)
        self.assertEqual(ei.render_passage_block([])["tags"], {})


class MaterialArrivalTests(IndexFixture):
    def test_arrivals_per_company_backfill_flag_and_append_only(self) -> None:
        backfill = ei.material_arrivals(self.state, company_ref=ACN, include_backfill=True)
        refs = {row["document_ref"] for row in backfill}
        self.assertTrue({self.refs["transcript"], self.refs["gs"], self.refs["8k"], self.refs["ex99"]} <= refs)
        self.assertEqual({row["backfill"] for row in backfill}, {1})  # acquired before the index existed
        self.assertEqual(ei.material_arrivals(self.state, company_ref=ACN), [])
        epam = {row["document_ref"] for row in ei.material_arrivals(self.state, company_ref=EPAM,
                                                                    include_backfill=True)}
        self.assertEqual(epam, {self.refs["wiki"], self.refs["guidepoint"]})
        summary = ei.material_arrival_summary(self.state, ACN, include_backfill=True)
        self.assertEqual(summary["count"], len(backfill))
        self.assertEqual(summary["by_kind"]["broker_report"], 2)
        # Re-indexing the same bytes adds nothing; the ledger refuses edits.
        ei.index_tick(self.state, max_documents=100)
        self.assertEqual(len(ei.material_arrivals(self.state, company_ref=ACN, include_backfill=True)),
                         len(backfill))
        connection = sqlite3.connect(ei.index_path(self.state))
        self.addCleanup(connection.close)
        with self.assertRaises(sqlite3.DatabaseError):
            connection.execute("UPDATE material_arrivals SET kind='x'")
        with self.assertRaises(sqlite3.DatabaseError):
            connection.execute("DELETE FROM material_arrivals")


class DeliverableNumberTests(IndexFixture):
    def section(self, numbers, body):
        return {"title": "业绩", "body": body, "numbers": numbers}

    def test_a_figure_in_a_cited_passage_is_sourced(self) -> None:
        from dalton_core.mission_deliverable import (
            MissionDeliverableConflict, unsourced_numbers, validate_section,
        )

        quote = "New bookings were $22.2 billion for the quarter, a 4% increase"
        start = TRANSCRIPT.index(quote)
        ref = ei.make_passage_ref(self.refs["transcript"], start, start + len(quote), quote)
        resolve = ei.passage_resolver(self.state)
        body = "本季新签订单 22.2 billion 美元，同比增长 4%。"
        section = validate_section(self.section(
            [{"text": "New bookings $22.2 billion, up 4%", "passage_ref": ref}], body), resolve_passage=resolve)
        self.assertEqual(section["numbers"][0]["passage_ref"], ref)
        self.assertEqual(unsourced_numbers(body, section["numbers"]), [])
        # A figure the passage does not say, a forged ref, no resolver: refused.
        with self.assertRaisesRegex(MissionDeliverableConflict, "not in the passage"):
            validate_section(self.section([{"text": "bookings $23.0 billion", "passage_ref": ref}],
                                          "订单 23.0 billion"), resolve_passage=resolve)
        forged = ref[:-12] + "0" * 12
        with self.assertRaisesRegex(MissionDeliverableConflict, "does not resolve"):
            validate_section(self.section([{"text": "bookings $22.2 billion", "passage_ref": forged}],
                                          "订单 22.2 billion"), resolve_passage=resolve)
        with self.assertRaisesRegex(MissionDeliverableConflict, "does not resolve"):
            validate_section(self.section([{"text": "bookings $22.2 billion", "passage_ref": ref}],
                                          "订单 22.2 billion"))
        # Claim-tag sourcing is unchanged: exactly one provenance per figure.
        from dalton_core.mission_deliverable import MissionDeliverableValidationError
        with self.assertRaisesRegex(MissionDeliverableValidationError, "exactly one of claim_version_ref and cell"):
            validate_section(self.section([{"text": "22.2", "passage_ref": ref, "claim_version_ref": "c"}],
                                          "22.2"), resolve_passage=resolve)

    def test_a_declared_derivation_of_cited_figures_is_sourced(self) -> None:
        from dalton_core.mission_deliverable import (
            MissionDeliverableConflict, unsourced_numbers, validate_section,
        )

        gs = "we model bookings of $21.0 billion"
        g_start = GS_REPORT.index(gs)
        gs_ref = ei.make_passage_ref(self.refs["gs"], g_start, g_start + len(gs), gs)
        quote = "New bookings were $22.2 billion"
        start = TRANSCRIPT.index(quote)
        ref = ei.make_passage_ref(self.refs["transcript"], start, start + len(quote), quote)
        resolve = ei.passage_resolver(self.state)
        numbers = [{"text": "实际 $22.2 billion", "passage_ref": ref},
                   {"text": "高盛预期 $21.0 billion", "passage_ref": gs_ref},
                   {"text": "超出高盛预期 5.7%", "derivation": {"expression": "(22.2 / 21.0 - 1) * 100",
                                                          "result": "5.7"}}]
        body = "订单 22.2 billion 美元，较高盛预期的 21.0 billion 高出 5.7%。"
        section = validate_section(self.section(numbers, body), resolve_passage=resolve)
        self.assertEqual(unsourced_numbers(body, section["numbers"]), [])
        wrong = [dict(numbers[0]), dict(numbers[1]),
                 {"text": "超出 6.9%", "derivation": {"expression": "(22.2 / 21.0 - 1) * 100", "result": "6.9"}}]
        with self.assertRaisesRegex(MissionDeliverableConflict, "does not compute"):
            validate_section(self.section(wrong, "高出 6.9%"), resolve_passage=resolve)
        uncited = [dict(numbers[0]),
                   {"text": "超出 5.7%", "derivation": {"expression": "(22.2 / 21.0 - 1) * 100", "result": "5.7"}}]
        with self.assertRaisesRegex(MissionDeliverableConflict, "no other figure"):
            validate_section(self.section(uncited, "高出 5.7%"), resolve_passage=resolve)

    def test_the_authority_resolves_passages_beside_its_core(self) -> None:
        from dalton_core.mission_deliverable import MissionDeliverableAuthority
        from dalton_core.store import DaltonStore

        with tempfile.TemporaryDirectory() as other:
            store = DaltonStore(Path(other) / "core.sqlite")
            self.addCleanup(store.close)
            authority = object.__new__(MissionDeliverableAuthority)
            authority.store = store
            self.assertIsNone(authority.passage_resolver()(f"doc-passage:{self.refs['gs']}:0-5:{'a' * 12}"))
        store = SimpleNamespace(path=str(self.state / "core.sqlite"))
        authority = object.__new__(MissionDeliverableAuthority)
        authority.store = store
        quote = "New bookings were $22.2 billion"
        start = TRANSCRIPT.index(quote)
        ref = ei.make_passage_ref(self.refs["transcript"], start, start + len(quote), quote)
        self.assertEqual(authority.passage_resolver()(ref), quote)


class LaneTests(IndexFixture):
    def test_the_lane_is_registered_in_code_and_indexes_on_tick(self) -> None:
        from dalton_core.document_index_lane import dispatch
        from dalton_core.lane_registry import LANE_MODULES, lane_for_operation, tick_lanes

        self.assertIn("dalton_core.document_index_lane", LANE_MODULES)
        spec = lane_for_operation("dispatch_document_index")
        self.assertEqual((spec.order, spec.driver_key, spec.init_kwarg, spec.launcher_factory),
                         (37, "document_index", None, None))
        self.assertIn(spec, tick_lanes())
        out = dispatch(SimpleNamespace(state_dir=self.state), {})
        self.assertEqual(out["status"], "idle", out)
        self.assertIn("没有新获取的资料", out["reason"])
        self.assertEqual(dispatch(SimpleNamespace(state_dir=None), {})["status"], "unconfigured")
        with tempfile.TemporaryDirectory() as blank:
            fresh = dispatch(SimpleNamespace(state_dir=Path(blank)), {})
            self.assertEqual(fresh["status"], "idle", fresh)
            self.assertTrue((Path(blank) / ei.INDEX_DB).is_file())


class BlankWorkspaceLaneCanary(unittest.TestCase):
    """A created workspace runs the lane from code with nothing configured."""

    def setUp(self) -> None:
        from tests.test_new_workspace_canary import NewWorkspaceCanaryTests

        NewWorkspaceCanaryTests.setUp(self)

    def test_lane_runs_in_a_new_workspace(self) -> None:
        from tests.test_new_workspace_canary import NewWorkspaceCanaryTests, NewWorkspaceOwnerLeverTests
        from dalton_core.document_index_lane import dispatch

        workspace, mission = NewWorkspaceCanaryTests.create_workspace(self)
        server = NewWorkspaceOwnerLeverTests.writer(self, workspace.state_dir)
        out = dispatch(server, {})
        self.assertEqual(out["status"], "idle", out)
        status = ei.index_status(workspace.state_dir)
        self.assertEqual((status["exists"], status["documents"]), (True, 0))
        found = ei.evidence_search(workspace.state_dir, "bookings")
        self.assertEqual(found["status"], "empty")


class CockpitEndpointTests(IndexFixture):
    def setUp(self) -> None:
        super().setUp()
        from dalton_core.cockpit_v2.charts_api import ChartReader

        self.reader = ChartReader(self.state / "core.sqlite")

    def get(self, path: str, context=None, **query):
        from dalton_core.cockpit_v2 import evidence_search_api as api

        status, content_type, body = api.evidence_search_routes()[path](query, context or self.reader)
        self.assertEqual(content_type, "application/json; charset=utf-8")
        return status, json.loads(body)

    def test_search_passages_and_passage(self) -> None:
        from dalton_core.cockpit_v2 import evidence_search_api as api

        status, body = self.get(api.SEARCH_PATH, q="bookings FY27 guidance", company="ACN", limit="5")
        self.assertEqual((status, body["status"]), (200, "ok"))
        self.assertLessEqual(len(body["results"]), 5)
        first = body["results"][0]
        self.assertIn("Accenture ACN", first["companies"])
        self.assertTrue(first["passage_ref"].startswith("doc-passage:"))
        status, body = self.get(api.PASSAGE_PATH, ref=first["passage_ref"])
        self.assertEqual((status, body["status"]), (200, "ok"))
        self.assertEqual(body["passage"]["text"], first["text"])
        status, body = self.get(api.PASSAGES_PATH, document=self.refs["gs"])
        self.assertEqual((status, body["status"], body["document"]["house"]), (200, "ok", "Goldman Sachs"))

    def test_honest_empty_states_and_bad_requests(self) -> None:
        from dalton_core.cockpit_v2 import evidence_search_api as api
        from dalton_core.cockpit_v2.charts_api import ChartReader

        status, body = self.get(api.SEARCH_PATH, q="bookings", company="ZZZZ")
        self.assertEqual((status, body["status"]), (200, "empty"))
        self.assertEqual(set(body["empty"]), {"state", "impact", "owner", "action"})
        status, body = self.get(api.SEARCH_PATH, q="zzqqxx")
        self.assertEqual((status, body["status"]), (200, "empty"))
        for query in ({}, {"q": "x", "kinds": "nonsense"}, {"q": "x", "limit": "0"}):
            status, body = self.get(api.SEARCH_PATH, **query)
            self.assertEqual(status, 400, query)
        status, _ = self.get(api.PASSAGE_PATH, ref="not-a-ref")
        self.assertEqual(status, 400)
        quote = "New bookings were $22.2 billion"
        start = TRANSCRIPT.index(quote)
        ref = ei.make_passage_ref(self.refs["transcript"], start, start + len(quote), quote)
        status, body = self.get(api.PASSAGE_PATH, ref=ref[:-12] + "0" * 12)
        self.assertEqual((status, body["status"]), (200, "empty"))
        self.assertIn("指纹", body["empty"]["state"])
        with tempfile.TemporaryDirectory() as blank:
            reader = ChartReader(Path(blank) / "core.sqlite")
            for path, query in ((api.SEARCH_PATH, {"q": "bookings"}), (api.PASSAGES_PATH, {"document": "x"}),
                                (api.PASSAGE_PATH, {"ref": ref})):
                status, body = self.get(path, reader, **query)
                self.assertEqual((status, body["status"]), (200, "empty"), path)
            # A Core with a mission and no index yet.
            core = Path(blank) / "core.sqlite"
            connection = sqlite3.connect(core)
            connection.executescript(CORE_SCHEMA)
            connection.execute("INSERT INTO coverage_mission_versions VALUES (?,?,?,?)",
                               (MISSION["id"], MISSION["mission_ref"], 1, json.dumps(MISSION)))
            connection.execute("INSERT INTO coverage_mission_pointer VALUES (?,?,?)",
                               (MISSION["mission_ref"], MISSION["id"], "t"))
            connection.commit()
            connection.close()
            status, body = self.get(api.SEARCH_PATH, ChartReader(core), q="bookings")
            self.assertEqual((status, body["status"]), (200, "empty"))
            self.assertIn("全文证据索引", body["empty"]["state"])
        status, body = self.get(api.SEARCH_PATH, object(), q="bookings")
        self.assertEqual((status, body["status"]), (200, "empty"))

    def test_routes_are_served_through_the_v2_router(self) -> None:
        from dalton_core.cockpit_v2 import evidence_search_api as api
        from dalton_core.cockpit_v2.routes import register_v2_routes

        router = register_v2_routes(SimpleNamespace())
        for path in (api.SEARCH_PATH, api.PASSAGES_PATH, api.PASSAGE_PATH):
            self.assertIn(path, router.raw_gets)


if __name__ == "__main__":
    unittest.main()
