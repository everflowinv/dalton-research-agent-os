"""Cockpit v2 evidence chain (batch ah; review C10/C13/A03): 「查看原文与出处」
opens the filed line or the cited passage, scoped to the mission, never a 5xx."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from dalton_core.cockpit_v2 import evidence_api as ev
from dalton_core.cockpit_v2.charts_api import ChartReader
from tests.p14a_fixtures import ACN, EPAM

OTHER = "company:sec-cik:0000000001"
MISSION = {"id": "coverage-mission-version:test:1", "mission_ref": "coverage-mission:test", "version": 1,
           "content_hash": "e" * 64, "industry_ref": "industry:test",
           "universe": [{"company_ref": ACN, "ticker": "ACN", "name": "Accenture"},
                        {"company_ref": EPAM, "ticker": "EPAM"}]}

SCHEMA = """
CREATE TABLE coverage_mission_versions(mission_version_id TEXT PRIMARY KEY, mission_ref TEXT,
    version_number INTEGER, record_json TEXT);
CREATE TABLE coverage_mission_pointer(mission_ref TEXT PRIMARY KEY, mission_version_id TEXT, updated_at TEXT);
CREATE TABLE claim_versions(claim_version_id TEXT PRIMARY KEY, claim_json TEXT, created_at TEXT);
CREATE TABLE evidence_versions(evidence_version_id TEXT PRIMARY KEY, evidence_ref TEXT, evidence_json TEXT,
    created_at TEXT);
CREATE TABLE evidence_relations(relation_id TEXT PRIMARY KEY, evidence_ref TEXT, evidence_version_id TEXT,
    claim_ref TEXT, claim_version_id TEXT, relation TEXT, relation_json TEXT, content_hash TEXT, created_at TEXT);
CREATE TABLE transcript_claim_citation_bindings(binding_id TEXT PRIMARY KEY, correction_set_version_ref TEXT,
    source_manifest_ref TEXT, source_manifest_hash TEXT, source_content_hash TEXT, source_start INTEGER,
    source_end INTEGER, claim_eligible INTEGER, record_json TEXT, content_hash TEXT, created_at TEXT);
CREATE TABLE transcript_correction_set_versions(version_id TEXT PRIMARY KEY, record_json TEXT);
CREATE TABLE document_provenance_records(document_ref TEXT PRIMARY KEY, source_ref TEXT, provenance_tier TEXT,
    broker TEXT, title TEXT, published_at TEXT, record_json TEXT);
CREATE TABLE coverage_mission_statement_filings(ingest_id TEXT PRIMARY KEY, company_ref TEXT, cik TEXT,
    entity_name TEXT, accession TEXT, form TEXT, filed TEXT, report_date TEXT);
CREATE TABLE coverage_mission_statement_lines(line_id TEXT PRIMARY KEY, ingest_id TEXT, statement TEXT,
    ordinal INTEGER, concept TEXT, label TEXT, level INTEGER, parent_concept TEXT, is_breakdown INTEGER,
    dimension_axis TEXT, dimension_member TEXT, period_start TEXT, period_end TEXT, value TEXT, unit TEXT);
CREATE TABLE claim_retirement_decisions(decision_id TEXT PRIMARY KEY, claim_version_ref TEXT, decision TEXT);
"""

INGEST = "statement-ingest:ca2f7c13d6ae3495a7b4340c85a72ab7"
LINE = f"{INGEST}#274"
SEC_CLAIM = "claim-version:" + "1" * 64
DOC_CLAIM = "claim-version:" + "2" * 64
OUT_CLAIM = "claim-version:" + "3" * 64
BARE_CLAIM = "claim-version:" + "4" * 64
TAMPERED_CLAIM = "claim-version:" + "5" * 64
DOCUMENT = "alphaengine-doc:320000610304599"
TEXT = ("Payments, Processors, & IT Services\nSunday Spotlight\n" + "Intro words here. " * 40
        + "Exhibit 10 shows the 2026 change in NTM P/E multiples for CTSH, ACN and EPAM. "
        + "More words follow after. " * 40)
CITED = "Exhibit 10 shows the 2026 change"
START = TEXT.index(CITED)
END = START + len(CITED)


def _put_object(state: Path, data: bytes) -> str:
    digest = hashlib.sha256(data).hexdigest()
    shard = state / "transcript-spool" / "connector-spool" / "objects" / digest[:2]
    shard.mkdir(parents=True, exist_ok=True)
    (shard / digest).write_bytes(data)
    return digest


class EvidenceFixture(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.state = Path(self.tmp.name)
        self.core = self.state / "core.sqlite"
        db = sqlite3.connect(self.core)
        db.executescript(SCHEMA)
        db.execute("INSERT INTO coverage_mission_versions VALUES (?,?,?,?)",
                   (MISSION["id"], MISSION["mission_ref"], 1, json.dumps(MISSION)))
        db.execute("INSERT INTO coverage_mission_pointer VALUES (?,?,?)",
                   (MISSION["mission_ref"], MISSION["id"], "2026-10-01T00:00:00Z"))
        digest = _put_object(self.state, TEXT.encode("utf-8"))
        # a document whose stored bytes no longer hash to what was bound
        bad = hashlib.sha256(b"what was cited").hexdigest()
        shard = self.state / "transcript-spool" / "connector-spool" / "objects" / bad[:2]
        shard.mkdir(parents=True, exist_ok=True)
        (shard / bad).write_bytes(b"something else entirely")

        def claim(ref, subject, statement, **extra):
            body = {"id": ref, "claim_ref": "claim:" + ref[-8:], "subject_ref": subject,
                    "normalized_statement": statement, "period": "2024-09-01..2024-11-30",
                    "claim_kind": "quantitative" if extra.get("value") else "qualitative", **extra}
            db.execute("INSERT INTO claim_versions VALUES (?,?,?)", (ref, json.dumps(body), "2026-10-01T00:00:00Z"))

        def evidence(claim_ref, n, body, relation="supports"):
            eid = f"evidence-version:{n}"
            db.execute("INSERT INTO evidence_versions VALUES (?,?,?,?)",
                       (eid, f"evidence:{n}", json.dumps({"id": eid, **body}), "2026-10-01T00:00:00Z"))
            db.execute("INSERT INTO evidence_relations VALUES (?,?,?,?,?,?,?,?,?)",
                       (f"relation:{n}", f"evidence:{n}", eid, "claim:x", claim_ref, relation, "{}", "h",
                        "2026-10-01T00:00:00Z"))

        claim(SEC_CLAIM, ACN, "Accenture plc在10-Q（accession 0001467373-24-000352）中列报的营业收入（按行业："
              "Financial Services），期间 2024-09-01..2024-11-30，为 USD 3168835000。该数字直接取自申报的 XBRL 行 "
              "us-gaap:Revenues（Financial Services，第 274 行），未经任何换算。",
              value="3168835000", unit="USD", currency="USD")
        evidence(SEC_CLAIM, 1, {"source_ref": "source:sec-edgar", "source_type": "official_filing",
                                "artifact_refs": [{"ref": "raw-sink:" + "a" * 64, "hash": "a" * 64}],
                                "source_lineage": ["source:sec-edgar", INGEST,
                                                   "sec:filing:0001467373-24-000352", LINE]})
        db.execute("INSERT INTO coverage_mission_statement_filings VALUES (?,?,?,?,?,?,?,?)",
                   (INGEST, ACN, "0001467373", "Accenture plc", "0001467373-24-000352", "10-Q",
                    "2024-12-19", "2024-11-30"))
        for ordinal, concept, label, value in ((273, "us-gaap:Revenues", "Revenues", "17690000000"),
                                               (274, "us-gaap:Revenues", "Financial Services", "3168835000"),
                                               (275, "us-gaap:CostOfRevenue", "Cost of services", "12000000000")):
            db.execute("INSERT INTO coverage_mission_statement_lines VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                       (f"{INGEST}#{ordinal}", INGEST, "income", ordinal, concept, label, 3, None, 0, None,
                        "us-gaap:FinancialServicesSectorMember" if ordinal == 274 else None,
                        "2024-09-01", "2024-11-30", value, "usd"))

        claim(DOC_CLAIM, EPAM, "For 2026, Cognizant, Accenture and EPAM showed YTD declines in NTM P/E.")
        binding = "transcript-claim-citation-binding:" + "b" * 32
        db.execute("INSERT INTO transcript_claim_citation_bindings VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                   (binding, "transcript-correction-set-version:c1", "m", "h", digest, START, END, 1, "{}", "h", "t"))
        db.execute("INSERT INTO transcript_correction_set_versions VALUES (?,?)",
                   ("transcript-correction-set-version:c1", json.dumps({"document_ref": DOCUMENT})))
        db.execute("INSERT INTO document_provenance_records VALUES (?,?,?,?,?,?,?)",
                   (DOCUMENT, "source:alphaengine", "sell_side", "Wolfe Research", "Sunday Spotlight",
                    "2026-09-06 08:00:00", json.dumps({"spec_ref": "sell-side-research"})))
        evidence(DOC_CLAIM, 2, {"source_ref": "source:alphaengine", "source_type": "authenticated_transcript",
                                "artifact_refs": [{"ref": binding, "hash": "h"}],
                                "source_lineage": ["source:alphaengine", binding]})

        claim(TAMPERED_CLAIM, ACN, "A statement whose source bytes changed.")
        binding2 = "transcript-claim-citation-binding:" + "c" * 32
        db.execute("INSERT INTO transcript_claim_citation_bindings VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                   (binding2, "transcript-correction-set-version:c1", "m", "h", bad, 0, 5, 1, "{}", "h", "t"))
        evidence(TAMPERED_CLAIM, 3, {"source_ref": "source:alphaengine", "source_type": "authenticated_transcript",
                                     "artifact_refs": [{"ref": binding2, "hash": "h"}], "source_lineage": []})

        claim(OUT_CLAIM, OTHER, "A company outside the mission.")
        claim(BARE_CLAIM, ACN, "A conclusion with no recorded evidence.")
        db.commit()
        db.close()
        self.reader = ChartReader(self.core)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    def get(self, path: str, context=None, **query) -> tuple[int, dict]:
        status, content_type, body = ev.evidence_routes()[path](query, context or self.reader)
        self.assertEqual(content_type, ev.JSON_TYPE)
        return status, json.loads(body)

    @staticmethod
    def visible(value) -> str:
        def strip(x):
            if isinstance(x, dict):
                return {k: strip(v) for k, v in x.items() if k != "technical"}
            if isinstance(x, list):
                return [strip(v) for v in x]
            return x
        return json.dumps(strip(value), ensure_ascii=False)


class SourceTests(EvidenceFixture):
    def test_sec_line_opens_the_filed_line_and_the_edgar_filing(self) -> None:
        status, value = self.get(ev.SOURCE_PATH, claim=SEC_CLAIM)
        self.assertEqual((status, value["status"]), (200, "ok"))
        self.assertEqual(value["button_label"], "查看原文与出处")
        self.assertTrue(value["available_original"])
        self.assertEqual(value["claim"]["company"], "Accenture ACN")
        source = value["sources"][0]
        self.assertEqual(source["source_kind"], "sec_statement_line")
        self.assertEqual(source["document_type_label"], "季报 10-Q")
        self.assertEqual(source["relation_label"], "支持")
        self.assertEqual(source["filing"]["accession"], "0001467373-24-000352")
        self.assertEqual(source["location"]["label"], "利润表 第 274 行：营业收入（Financial Services）")
        self.assertIn("31.69 亿美元", source["passage"]["text"])
        original = [link for link in source["links"] if link["kind"] == "original"][0]
        self.assertEqual(original["url"], "https://www.sec.gov/Archives/edgar/data/1467373/"
                                          "000146737324000352/0001467373-24-000352-index.htm")
        self.assertIn("SEC EDGAR", original["label"])
        visible = self.visible(value)
        self.assertNotIn("us-gaap:", visible)
        self.assertNotIn("claim-version:", visible.replace(SEC_CLAIM, ""))
        self.assertNotIn("evidence-version:", visible)
        self.assertEqual(value["technical"]["claim_ref"], SEC_CLAIM)
        self.assertEqual(source["technical"]["concept"], "us-gaap:Revenues")

    def test_an_xbrl_company_fact_opens_its_filed_line_or_the_filing(self) -> None:
        """Batch ah: an SEC figure read from the company-facts API names its
        filing through the envelope's ``sec:company-concept:`` records."""
        db = sqlite3.connect(self.core)
        db.execute("CREATE TABLE connector_source_envelopes(source_envelope_id TEXT PRIMARY KEY, record_json TEXT)")
        db.execute("INSERT INTO connector_source_envelopes VALUES (?,?)", ("source-envelope:xbrl", json.dumps({
            "source_record_refs": ["sec:company-concept:0001467373-24-000352:us-gaap:Revenues:CY2024Q4"]})))
        for ref, n, period in (("claim-version:" + "7" * 64, 7, "2024-09-01..2024-11-30"),
                               ("claim-version:" + "8" * 64, 8, "2023-09-01..2023-11-30")):
            db.execute("INSERT INTO claim_versions VALUES (?,?,?)", (ref, json.dumps({
                "id": ref, "subject_ref": ACN, "normalized_statement": "Accenture plc reported Revenues of USD 17690000000.",
                "period": period, "claim_kind": "quantitative", "value": "17690000000", "unit": "USD"}), "2026-10-01T00:00:00Z"))
            eid = f"evidence-version:{n}"
            db.execute("INSERT INTO evidence_versions VALUES (?,?,?,?)", (eid, f"evidence:{n}", json.dumps({
                "id": eid, "source_ref": "source:sec-edgar", "source_type": "official_filing",
                "source_envelope_ref": "source-envelope:xbrl",
                "source_lineage": ["source:sec-edgar", "source-envelope:xbrl", "artifact-version:x"]}), "t"))
            db.execute("INSERT INTO evidence_relations VALUES (?,?,?,?,?,?,?,?,?)",
                       (f"relation:{n}", f"evidence:{n}", eid, "claim:x", ref, "supports", "{}", "h", "t"))
        db.commit()
        db.close()
        # the period matches one undimensioned line: the filed line itself
        status, value = self.get(ev.SOURCE_PATH, claim="claim-version:" + "7" * 64)
        self.assertEqual(value.get("status"), "ok", value.get("technical_reason"))
        source = value["sources"][0]
        self.assertEqual(source["source_kind"], "sec_statement_line")
        self.assertEqual(source["location"]["line_ordinal"], 273)
        self.assertTrue(value["available_original"])
        # no line for that period: the filing and its EDGAR link, said plainly
        status, value = self.get(ev.SOURCE_PATH, claim="claim-version:" + "8" * 64)
        self.assertEqual(value.get("status"), "ok", (value.get("technical_reason"), value.get("empty_reason")))
        source = value["sources"][0]
        self.assertEqual(source["source_kind"], "sec_xbrl")
        self.assertEqual(source["location"]["label"], "申报的 XBRL 数据：营业收入")
        self.assertIn("XBRL 结构化数据", source["passage_unavailable_reason"])
        self.assertTrue(any(link["kind"] == "original" and "edgar/data/1467373" in link["url"] for link in source["links"]))
        self.assertNotIn("us-gaap:", self.visible(value))

    def test_cited_passage_with_highlight_title_and_reader(self) -> None:
        _status, value = self.get(ev.SOURCE_PATH, claim=DOC_CLAIM)
        self.assertEqual(value["claim"]["company"], "EPAM")
        self.assertEqual(value["claim"]["statement_lang"], "en")
        source = value["sources"][0]
        self.assertEqual(source["document_type_label"], "卖方研报")
        self.assertEqual((source["title"], source["house"], source["published_at"]),
                         ("Sunday Spotlight", "Wolfe Research", "2026-09-06"))
        passage = source["passage"]
        lo, hi = passage["highlight"]
        self.assertIn(CITED, passage["text"][lo:hi])
        self.assertLess(len(passage["text"]), 1200 + 2 * ev.PASSAGE_CONTEXT)
        self.assertRegex(source["location"]["label"], r"^原文第 [\d,]+–[\d,]+ 字（第 \d+ 行）$")
        reader = [link for link in source["links"] if link["kind"] == "reader"][0]
        self.assertEqual(reader["href"], f"{ev.DOCUMENT_PATH}?claim={DOC_CLAIM}&source=0")
        self.assertEqual(source["technical"]["document_ref"], DOCUMENT)
        self.assertNotIn(DOCUMENT, self.visible(value))

    def test_tampered_bytes_show_no_passage_and_say_why(self) -> None:
        _status, value = self.get(ev.SOURCE_PATH, claim=TAMPERED_CLAIM)
        source = value["sources"][0]
        self.assertIsNone(source["passage"])
        self.assertIn("指纹不一致", source["passage_unavailable_reason"])
        self.assertEqual(value["button_label"], "查看结论记录")
        self.assertFalse(any(link["kind"] == "reader" for link in source["links"]))

    def test_claim_outside_the_mission_or_retired_is_empty(self) -> None:
        for ref in (OUT_CLAIM, "claim-version:none"):
            status, value = self.get(ev.SOURCE_PATH, claim=ref)
            self.assertEqual((status, value["status"]), (200, "empty"))
            self.assertIn("不在当前研究项目中", value["empty_reason"])
        with sqlite3.connect(self.core) as db:
            db.execute("INSERT INTO claim_retirement_decisions VALUES ('d', ?, 'retired')", (SEC_CLAIM,))
        _status, value = self.get(ev.SOURCE_PATH, claim=SEC_CLAIM)
        self.assertEqual(value["status"], "empty")

    def test_a_conclusion_without_evidence_says_so(self) -> None:
        _status, value = self.get(ev.SOURCE_PATH, claim=BARE_CLAIM)
        self.assertEqual((value["status"], value["sources"]), ("ok", []))
        self.assertEqual(value["button_label"], "查看结论记录")
        self.assertEqual(set(value["sources_empty"]), {"state", "impact", "owner", "action"})

    def test_missing_parameter_is_a_400_in_chinese(self) -> None:
        for path in (ev.SOURCE_PATH, ev.DOCUMENT_PATH):
            status, value = self.get(path)
            self.assertEqual(status, 400)
            self.assertRegex(value["display_message"], r"[一-鿿]")
        status, _value = self.get(ev.SOURCE_PATH, claim="x" * 600)
        self.assertEqual(status, 400)
        status, _value = self.get(ev.DOCUMENT_PATH, claim=DOC_CLAIM, source="abc")
        self.assertEqual(status, 400)


class BlankWorkspaceTests(unittest.TestCase):
    def test_no_core_and_no_mission_are_honest_empty_states(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            reader = ChartReader(Path(tmp) / "core.sqlite")
            for path, query in ((ev.SOURCE_PATH, {"claim": SEC_CLAIM}),
                                (ev.DOCUMENT_PATH, {"claim": SEC_CLAIM}),
                                (ev.STATEMENT_LINE_PATH, {"line": LINE})):
                status, _type, body = ev.evidence_routes()[path](query, reader)
                value = json.loads(body)
                self.assertEqual((status, value["status"]), (200, "empty"))
                self.assertEqual(set(value["empty"]), {"state", "impact", "owner", "action"})
                for word in ("当前状态", "影响", "谁处理", "你可以"):
                    self.assertIn(word, value["empty_reason"])
            sqlite3.connect(Path(tmp) / "core.sqlite").close()
            status, _type, body = ev.evidence_routes()[ev.SOURCE_PATH]({"claim": SEC_CLAIM}, reader)
            self.assertEqual(json.loads(body)["empty"]["state"], "这个研究环境还没有发布研究目标")

    def test_an_unconfigured_context_is_not_a_server_error(self) -> None:
        status, _type, body = ev.evidence_routes()[ev.SOURCE_PATH]({"claim": SEC_CLAIM}, object())
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["status"], "empty")


class DocumentTests(EvidenceFixture):
    def test_full_text_with_the_cited_span(self) -> None:
        _status, value = self.get(ev.DOCUMENT_PATH, claim=DOC_CLAIM, source="0")
        self.assertEqual(value["status"], "ok")
        self.assertEqual(value["text"], TEXT)
        self.assertFalse(value["truncated"])
        # widened to the whole sentence the citation cuts
        self.assertIn(CITED, value["text"][value["highlight"]["start"]:value["highlight"]["end"]])
        self.assertEqual(value["title"], "Sunday Spotlight")

    def test_a_long_document_is_cut_around_the_citation(self) -> None:
        original = ev.MAX_DOCUMENT_CHARS
        ev.MAX_DOCUMENT_CHARS = 200
        try:
            _status, value = self.get(ev.DOCUMENT_PATH, claim=DOC_CLAIM, source="0")
        finally:
            ev.MAX_DOCUMENT_CHARS = original
        self.assertTrue(value["truncated"])
        self.assertEqual(len(value["text"]), 200)
        # widened to the whole sentence the citation cuts
        self.assertIn(CITED, value["text"][value["highlight"]["start"]:value["highlight"]["end"]])

    def test_sec_source_reads_as_the_filed_statement(self) -> None:
        _status, value = self.get(ev.DOCUMENT_PATH, claim=SEC_CLAIM, source="0")
        self.assertEqual(value["statement_label"], "利润表")
        self.assertEqual([row["ordinal"] for row in value["rows"]], [273, 274, 275])
        self.assertEqual([row["cited"] for row in value["rows"]], [False, True, False])
        self.assertNotIn("us-gaap:", self.visible(value))

    def test_unknown_source_index_and_unreadable_text_are_empty(self) -> None:
        _status, value = self.get(ev.DOCUMENT_PATH, claim=DOC_CLAIM, source="5")
        self.assertEqual(value["status"], "empty")
        _status, value = self.get(ev.DOCUMENT_PATH, claim=TAMPERED_CLAIM, source="0")
        self.assertEqual(value["status"], "empty")


class StatementLineTests(EvidenceFixture):
    def test_line_by_id_and_by_filing_concept(self) -> None:
        _status, value = self.get(ev.STATEMENT_LINE_PATH, line=f"statement-line:{LINE}")
        self.assertEqual(value["status"], "ok")
        self.assertEqual(value["company"], "Accenture ACN")
        self.assertEqual(value["lines"][0]["claims"], [SEC_CLAIM])
        _status, value = self.get(ev.STATEMENT_LINE_PATH, company=ACN, accession="0001467373-24-000352",
                                  concept="us-gaap:Revenues")
        self.assertEqual([line["location"]["line_ordinal"] for line in value["lines"]], [273])
        self.assertEqual(value["lines"][0]["claims"], [])
        self.assertNotIn("us-gaap:", self.visible(value))

    def test_not_found_and_bad_accession(self) -> None:
        _status, value = self.get(ev.STATEMENT_LINE_PATH, line=f"{INGEST}#999")
        self.assertEqual(value["status"], "empty")
        self.assertIn("没有找到这一行", value["empty_reason"])
        status, _value = self.get(ev.STATEMENT_LINE_PATH, company=ACN, accession="nope", concept="x")
        self.assertEqual(status, 400)


class RegistrationTests(unittest.TestCase):
    def test_the_routes_are_served_through_the_v2_router(self) -> None:
        from dalton_core.cockpit_v2.routes import register_v2_routes

        router = register_v2_routes(SimpleNamespace())
        for path in (ev.SOURCE_PATH, ev.DOCUMENT_PATH, ev.STATEMENT_LINE_PATH):
            self.assertIn(path, router.raw_gets)
            self.assertIn(path, router.get_routes())


if __name__ == "__main__":
    unittest.main()
