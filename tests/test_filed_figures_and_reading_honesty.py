"""2026-10-09: "EPAM 已提取的文件数字为零 ... 电话会只读了 1 份".

Three projections the planner read wrong, and the fixes that make it see:

* segment figures a filer keeps in its notes (IBM) were dropped by the
  statements normaliser; EPAM's, on the face of its income statement, were
  held but invisible to the planner (``statement_segments``);
* where each company's reading stands and what holds an open review
  (``research_reading_pipeline``);
* both carried in the planner's per-company state (``research_state``).

Fixtures have the shape of the live parses (EPAM / IBM 10-Q, Q2 2026),
anonymised and cut to the lines that matter.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from dalton_core.raw_spool import RawSpool
from dalton_core.research_reading_pipeline import reading_pipeline
from dalton_core.research_state import company_state
from dalton_core.sec_financials_normalise import SEGMENT_AXIS, normalise_filing
from dalton_core.statement_segments import segment_summary, statement_segment_figures
from dalton_core.store import canonical_json


def _structure(concept: str, label: str, *, level: int = 4, axis=None, member=None) -> dict:
    return {"abstract": False, "concept": concept.replace(":", "_", 1), "label": label,
            "level": level, "parent_concept": "us-gaap_GrossProfit", "balance": "credit",
            "dimension_axis": axis, "dimension_member": member, "is_breakdown": False}


def _fact(concept: str, value: str, *, start="2026-04-01", end="2026-06-30", **dims) -> dict:
    row = {"concept": concept, "value": value, "unit_ref": "usd", "period_start": start,
           "period_end": end, "balance": "credit",
           "dim_us-gaap_StatementBusinessSegmentsAxis": None,
           "dim_srt_ConsolidationItemsAxis": None, "dim_srt_StatementGeographicalAxis": None}
    row.update(dims)
    projected = [(k, v) for k, v in dims.items() if v]
    if projected:
        row["dimension"] = projected[0][0].removeprefix("dim_").replace("_", ":", 1)
        row["member"] = projected[0][1]
    return row


SEGMENT = "dim_us-gaap_StatementBusinessSegmentsAxis"
CONSOLIDATION = "dim_srt_ConsolidationItemsAxis"
GEOGRAPHY = "dim_srt_StatementGeographicalAxis"

#: IBM's shape: the income statement presents Revenue and pre-tax income, and
#: the parse's income facts carry the segment table (segment axis plus the
#: consolidation axis set to operating segments) with no segment row in the
#: structure.
NOTES_FILER = {
    "income": {
        "structure": [
            _structure("us-gaap:Revenues", "Revenue"),
            _structure("us-gaap:IncomeLossFromContinuingOperationsBeforeIncomeTaxes"
                       "ExtraordinaryItemsNoncontrollingInterest",
                       "Income from continuing operations before income taxes", level=3),
        ],
        "facts": [
            _fact("us-gaap:Revenues", "17162000000"),
            _fact("us-gaap:Revenues", "5327000000",
                  **{SEGMENT: "issuer:ConsultingMember",
                     CONSOLIDATION: "us-gaap:OperatingSegmentsMember"}),
            _fact("us-gaap:Revenues", "7761000000",
                  **{SEGMENT: "issuer:SoftwareMember",
                     CONSOLIDATION: "us-gaap:OperatingSegmentsMember"}),
            _fact("us-gaap:IncomeLossFromContinuingOperationsBeforeIncomeTaxes"
                  "ExtraordinaryItemsNoncontrollingInterest", "647000000",
                  **{SEGMENT: "issuer:ConsultingMember",
                     CONSOLIDATION: "us-gaap:OperatingSegmentsMember"}),
            # Not segment figures: an elimination row, a segment-by-geography
            # cross, and a concept that is not a line of this statement.
            _fact("us-gaap:Revenues", "-90000000",
                  **{SEGMENT: "issuer:ConsultingMember",
                     CONSOLIDATION: "us-gaap:IntersegmentEliminationMember"}),
            _fact("us-gaap:Revenues", "2000000000",
                  **{SEGMENT: "issuer:ConsultingMember", GEOGRAPHY: "srt:AmericasMember"}),
            _fact("us-gaap:Goodwill", "10369000000", **{SEGMENT: "issuer:ConsultingMember"}),
        ],
    },
}

#: EPAM's shape: the segment rows are in the statement structure already.
FACE_FILER = {
    "income": {
        "structure": [
            _structure("us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues"),
            _structure("us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax", "Americas",
                       axis="us-gaap_StatementBusinessSegmentsAxis",
                       member="issuer_AmericasSegmentMember"),
        ],
        "facts": [
            _fact("us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax", "1414767000"),
            _fact("us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax", "792262000",
                  **{SEGMENT: "issuer:AmericasSegmentMember"}),
        ],
    },
}


def _filing(statements: dict) -> dict:
    return normalise_filing(accession="0000000000-26-000001", form="10-Q",
                            filed="2026-07-29", report_date="2026-06-30",
                            statements=statements)


class SegmentLineTests(unittest.TestCase):
    def test_a_notes_filers_segment_table_becomes_breakdown_lines_with_values(self) -> None:
        lines = [line for line in _filing(NOTES_FILER)["lines"]
                 if line["dimension_axis"] == SEGMENT_AXIS]
        got = {(line["concept"].split(":")[1][:20], line["dimension_member"], line["value"])
               for line in lines}
        self.assertEqual(got, {
            ("Revenues", "issuer:ConsultingMember", "5327000000"),
            ("Revenues", "issuer:SoftwareMember", "7761000000"),
            ("IncomeLossFromContin", "issuer:ConsultingMember", "647000000"),
        })
        consulting = next(line for line in lines if line["value"] == "5327000000")
        self.assertEqual(consulting["label"], "Revenue — Consulting")
        self.assertEqual((consulting["period_start"], consulting["period_end"], consulting["unit"]),
                         ("2026-04-01", "2026-06-30", "usd"))
        self.assertTrue(consulting["is_breakdown"])
        self.assertEqual((consulting["level"], consulting["parent_concept"]),
                         (5, "us-gaap:Revenues"))
        self.assertEqual(consulting["dimension_count"], 2)

    def test_a_face_filers_segment_rows_are_not_duplicated(self) -> None:
        lines = [line for line in _filing(FACE_FILER)["lines"]
                 if line["dimension_axis"] == SEGMENT_AXIS]
        self.assertEqual([(line["dimension_member"], line["value"]) for line in lines],
                         [("issuer:AmericasSegmentMember", "792262000")])


class SegmentProjectionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name)
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.addCleanup(self.connection.close)
        self.connection.executescript(
            "CREATE TABLE coverage_mission_statement_filings(ingest_id TEXT, company_ref TEXT,"
            " accession TEXT, form TEXT, report_date TEXT, source_record_refs_json TEXT);"
            "CREATE TABLE coverage_mission_statement_lines(ingest_id TEXT, ordinal INTEGER,"
            " statement TEXT, concept TEXT, label TEXT, dimension_axis TEXT, dimension_member TEXT,"
            " dimension_count INTEGER, period_start TEXT, period_end TEXT, value TEXT, unit TEXT);")

    def _spool(self, statements: dict) -> str:
        payload = canonical_json({"cik": "0000000001", "entity_name": "Issuer", "filings": [{
            "accession": "0000000000-26-000001", "form": "10-Q", "filed": "2026-07-29",
            "report_date": "2026-06-30", "statements": statements}]}).encode("utf-8")
        digest = hashlib.sha256(payload).hexdigest()
        sink = RawSpool(str(self.state / "connector-spool"), max_total_bytes=10_000_000).open_sink(
            f"raw-sink:{digest}", max_response_bytes=10_000_000)
        sink.write(payload)
        sink.finalize()
        return digest

    def test_held_filings_without_ledger_segments_are_read_from_their_recorded_parse(self) -> None:
        digest = self._spool(NOTES_FILER)
        self.connection.execute(
            "INSERT INTO coverage_mission_statement_filings VALUES(?,?,?,?,?,?)",
            ("ingest:1", "company:issuer", "0000000000-26-000001", "10-Q", "2026-06-30",
             json.dumps([f"raw-sink:{digest}"])))
        figures = statement_segment_figures(self.connection, "company:issuer",
                                            state_dir=self.state)
        self.assertEqual(figures["filings"][0]["basis"], "recorded_parse")
        summary = segment_summary(figures)
        self.assertEqual(summary["read_from_recorded_parse"], 1)
        latest = summary["latest_quarter"]
        consulting = next(item for item in latest["segments"]
                          if item["member"] == "issuer:ConsultingMember")
        self.assertEqual((consulting["segment"], consulting["revenue"]["value"],
                          consulting["profit"]["value"], consulting["revenue"]["unit"]),
                         ("Consulting", "5327000000", "647000000", "usd"))

    def test_a_tampered_parse_is_refused_not_read(self) -> None:
        digest = self._spool(NOTES_FILER)
        self.connection.execute(
            "INSERT INTO coverage_mission_statement_filings VALUES(?,?,?,?,?,?)",
            ("ingest:1", "company:issuer", "0000000000-26-000001", "10-Q", "2026-06-30",
             json.dumps(["raw-sink:" + "0" * 64])))
        figures = statement_segment_figures(self.connection, "company:issuer",
                                            state_dir=self.state)
        self.assertEqual(figures["filings"][0]["lines"], [])
        self.assertTrue(figures["filings"][0]["unreadable"])
        self.assertEqual(segment_summary(figures)["filings_with_segment_lines"], 0)
        self.assertTrue(digest)

    def test_ledger_segment_lines_are_used_as_recorded(self) -> None:
        self.connection.execute(
            "INSERT INTO coverage_mission_statement_filings VALUES(?,?,?,?,?,?)",
            ("ingest:2", "company:face", "0000000000-26-000002", "10-Q", "2026-06-30", "[]"))
        self.connection.execute(
            "INSERT INTO coverage_mission_statement_lines VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            ("ingest:2", 7, "income", "us-gaap:RevenueFromContractWithCustomerExcludingAssessedTax",
             "Americas", SEGMENT_AXIS, "issuer:AmericasSegmentMember", 1, "2026-04-01",
             "2026-06-30", "792262000", "usd"))
        summary = segment_summary(statement_segment_figures(
            self.connection, "company:face", state_dir=self.state))
        self.assertEqual(summary["latest_quarter"]["basis"], "ledger")
        self.assertEqual(summary["latest_quarter"]["segments"][0]["revenue"]["value"], "792262000")


class ReadingPipelineTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name)
        self.connection = sqlite3.connect(":memory:")
        self.connection.row_factory = sqlite3.Row
        self.addCleanup(self.connection.close)
        self.connection.executescript(
            "CREATE TABLE coverage_mission_versions(mission_version_id TEXT, mission_ref TEXT);"
            "CREATE TABLE coverage_mission_document_reviews(review_id TEXT, mission_version_ref TEXT,"
            " company_ref TEXT, document_ref TEXT, discovered_document_ref TEXT, state TEXT,"
            " rationale TEXT);"
            "CREATE TABLE coverage_mission_discovered_documents(record_id TEXT, discovery_ref TEXT);"
            "CREATE TABLE coverage_mission_source_discoveries(record_id TEXT, spec_ref TEXT);"
            "CREATE TABLE document_extraction_window_exclusions(review_id TEXT, error_code TEXT,"
            " failure_class TEXT);")
        for version in ("v1", "v2"):
            self.connection.execute("INSERT INTO coverage_mission_versions VALUES(?,?)",
                                    (version, "mission"))
        self.connection.execute("INSERT INTO coverage_mission_source_discoveries VALUES('s','earnings-call-transcripts')")
        rows = [
            ("r-open", "v2", "doc:q4", "awaiting_human_extraction", None),
            ("r-dead", "v2", "doc:q3", "awaiting_human_extraction", None),
            ("r-staged", "v2", "doc:q2", "extraction_staged", "ADR-0005 policy admission"),
            ("r-wrong", "v2", "doc:x", "dismissed", "P13i: document never names this company"),
            ("r-old", "v1", "doc:q2", "awaiting_human_extraction", None),
        ]
        for review_id, version, document, state, rationale in rows:
            self.connection.execute("INSERT INTO coverage_mission_discovered_documents VALUES(?, 's')",
                                    (f"d-{review_id}",))
            self.connection.execute(
                "INSERT INTO coverage_mission_document_reviews VALUES(?,?,?,?,?,?,?)",
                (review_id, version, "company:acn", document, f"d-{review_id}", state, rationale))
        self.connection.execute(
            "INSERT INTO document_extraction_window_exclusions VALUES('r-dead','MODEL_ADAPTER_REJECTED','permanent')")
        run = self.state / "extractions" / "abc"
        run.mkdir(parents=True)
        (self.state / "extractions" / "latest.json").write_text(json.dumps(
            {"ticket": "document-extraction:abc", "settled": True}))
        (run / "summary.json").write_text(json.dumps({
            "created_at": "2026-10-09T15:00:00+00:00", "status": "succeeded",
            "stop_reason": "drained", "reviews_scanned": 2, "reviews_complete": 0,
            "skipped": [], "redriven_windows": [{"review_id": "r-open", "status": "succeeded"}]}))

    def test_counts_by_kind_blockers_and_superseded_history(self) -> None:
        company = reading_pipeline(self.connection, "v2", state_dir=self.state)["company:acn"]
        kind = company["by_kind"]["earnings-call-transcripts"]
        self.assertEqual((kind["open"], kind["staged"], kind["dismissed"]),
                         (2, 1, {"not_about_company": 1}))
        self.assertEqual(company["superseded_open_rows"], 1)
        blockers = {item["document_ref"]: item["blocker"] for item in company["open_blockers"]}
        self.assertIn("re-driven", blockers["doc:q4"])
        self.assertIn("MODEL_ADAPTER_REJECTED/permanent", blockers["doc:q3"])
        self.assertEqual(company["last_run"]["stop_reason"], "drained")


class PlannerStateTests(unittest.TestCase):
    ENTRY = {"company_ref": "company:x", "items": [
        {"item_ref": "earnings_calls", "required": 4, "have": 4, "read": 4,
         "legacy_extracted": 0, "status": "complete"}]}

    def test_the_new_fields_travel_only_when_provided(self) -> None:
        plain = company_state({"company_ref": "company:x", "items": [
            {"item_ref": "earnings_calls", "required": 4, "have": 4, "read": 1}]})
        self.assertNotIn("filed_statement_figures", plain)
        self.assertNotIn("reading_pipeline", plain)
        self.assertNotIn("read_without_proof", plain["items"][0])
        full = company_state(self.ENTRY, filed_statement_figures={"segment_lines": 3},
                             reading_pipeline={"by_kind": {}})
        self.assertEqual(full["filed_statement_figures"], {"segment_lines": 3})
        self.assertEqual(full["reading_pipeline"], {"by_kind": {}})
        self.assertEqual(full["items"][0]["read_without_proof"], 0)

    def test_the_prompt_names_the_fields_and_forbids_the_vague_assessment(self) -> None:
        from dalton_core.research_planner import PROMPT_PROJECTION_RULE, build_prompt

        self.assertIn("filed_statement_figures", PROMPT_PROJECTION_RULE["company_core_fields"])
        self.assertIn("reading_pipeline", PROMPT_PROJECTION_RULE["company_core_fields"])
        prompt = build_prompt({"companies": [], "totals": {}})
        self.assertIn("not a human gate", prompt)
        self.assertIn("Assess incomplete reading/figures by the exact automatic step and cause, never vague depth", prompt)
        self.assertIn("Never order `extract_figures` for quarterly_financials lines/segments already held there", prompt)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
