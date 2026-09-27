"""Lanes must work for ``company:ticker:`` refs, not only legacy ``company:sec-cik:``.

A workspace created through setup names its companies ``company:ticker:amzn``
and resolves each CIK once, at first publish.  The ownership lane skipped every
such company (ws-7d: "every ownership filing in the window has been read" over
an empty universe), the catalyst lane never asked SEC for a date, and the
buyback / insider-plan readers asked the document index by a ref it never
files SEC documents under.  And the annual-research diagnostic asked for
``source:sec-filings``, a source ref no review has ever carried.
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from dalton_core.buyback_disclosure import buyback_documents
from dalton_core.mission_company_cik import company_cik, index_company_refs
from dalton_core.mission_catalyst_lane import _universe as catalyst_universe
from dalton_core.mission_ownership_lane import _universe as ownership_universe

WDGT = "company:ticker:wdgt"
CIK = "0000123456"
MISSION = {"universe": [
    {"company_ref": WDGT, "ticker": "WDGT", "bootstrap_priority": "P0"},
    {"company_ref": "company:sec-cik:0000051143", "ticker": "IBM", "bootstrap_priority": "P1"},
    {"company_ref": "company:ticker:nocik", "ticker": "NOCIK", "bootstrap_priority": "P2"},
]}


class CompanyCikTests(unittest.TestCase):
    def setUp(self) -> None:
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.state = Path(temp.name)
        with sqlite3.connect(self.state / "core.sqlite") as connection:
            # The CIK SEC stamped on the statement filings the mission ingested.
            connection.execute(
                "CREATE TABLE coverage_mission_statement_filings (company_ref TEXT, cik TEXT)")
            connection.execute("INSERT INTO coverage_mission_statement_filings VALUES(?,?)",
                               (WDGT, "123456"))
            schema = Path(__file__).resolve().parents[1] / "src/dalton_core/document_index_schema.sql"
            connection.executescript(schema.read_text(encoding="utf-8"))

    def test_the_ref_or_the_resolved_cik(self) -> None:
        self.assertEqual(company_cik("company:sec-cik:0001467373"), "0001467373")
        self.assertIsNone(company_cik(WDGT))
        self.assertEqual(company_cik(WDGT, state_dir=self.state), CIK)
        self.assertIsNone(company_cik("company:ticker:nocik", state_dir=self.state))

    def test_ownership_lane_covers_ticker_refs(self) -> None:
        rows = ownership_universe(MISSION, state_dir=self.state)
        self.assertEqual([(row["ticker"], row["issuer"]) for row in rows],
                         [("WDGT", CIK), ("IBM", "0000051143")])
        # Without the workspace, only the legacy-shaped ref -- the old behaviour.
        self.assertEqual([row["ticker"] for row in ownership_universe(MISSION)], ["IBM"])

    def test_catalyst_lane_gets_the_sec_half_for_ticker_refs(self) -> None:
        rows = {row["ticker"]: row["issuer"]
                for row in catalyst_universe(MISSION, state_dir=self.state)}
        self.assertEqual(rows, {"WDGT": CIK, "IBM": "0000051143", "NOCIK": ""})

    def test_buyback_reader_finds_filings_indexed_under_the_cik(self) -> None:
        connection = sqlite3.connect(self.state / "core.sqlite")
        connection.row_factory = sqlite3.Row
        self.addCleanup(connection.close)
        self.assertEqual(index_company_refs(connection, WDGT),
                         [WDGT, f"company:sec-cik:{CIK}"])
        connection.execute(
            "INSERT INTO document_index_documents(rowid,artifact_version_ref,"
            "artifact_version_hash,artifact_ref,artifact_version,artifact_content_hash,"
            "title,kind,media_type,access_class,source_record_refs_json,"
            "company_refs_json,document_date,source_metadata,extracted_text,"
            "extracted_text_ref,extracted_text_hash,extracted_text_size_bytes,"
            "input_ref,input_hash,record_json,content_hash) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (1, "artifact-version:1", "d" * 64, "artifact:1", 1, "e" * 64,
             "WDGT 10-Q for the quarter", "filing", "text/html", "public",
             json.dumps(["sec:filing:0000123456-26-000001"]),
             json.dumps([f"company:sec-cik:{CIK}"]), "2026-06-18", "{}", "text",
             "text:1", "f" * 64, 4, "input:1", "0" * 64, "{}", "1" * 64))
        # document_index files an SEC list_filings document by the issuer CIK.
        connection.execute("INSERT INTO document_index_companies VALUES(1, ?)",
                           (f"company:sec-cik:{CIK}",))
        found = buyback_documents(connection, company_ref=WDGT)
        self.assertEqual([row["accession"] for row in found], ["0000123456-26-000001"])


class AnnualEligibilitySourceTests(unittest.TestCase):
    def test_sec_reviews_are_found_under_the_source_ref_they_carry(self) -> None:
        from dalton_core.mission_annual_research_lane import MissionAnnualResearchCoordinator

        connection = sqlite3.connect(":memory:")
        connection.row_factory = sqlite3.Row
        self.addCleanup(connection.close)
        connection.executescript(
            "CREATE TABLE coverage_mission_document_reviews (review_id TEXT, company_ref TEXT,"
            " document_ref TEXT, state TEXT, source_ref TEXT, discovered_document_ref TEXT,"
            " created_at TEXT);"
            "CREATE TABLE coverage_mission_discovered_documents (record_id TEXT, status TEXT,"
            " ticket_ref TEXT);"
            "CREATE TABLE document_read_completion_proofs (proof_id TEXT, review_id TEXT);")
        connection.execute(
            "INSERT INTO coverage_mission_document_reviews VALUES(?,?,?,?,?,?,?)",
            ("review:1", WDGT, "sec:filing:0000123456-26-000002", "extraction_staged",
             "source:sec-edgar", "doc:1", "2026-09-01T00:00:00+00:00"))
        connection.execute("INSERT INTO coverage_mission_discovered_documents VALUES(?,?,?)",
                           ("doc:1", "acquired", "public-web-fetch:" + "a" * 24))
        store = type("Store", (), {"connection": connection})()
        lane = MissionAnnualResearchCoordinator(store=store, launcher=None)
        [candidate] = lane._annual_eligibility()["candidates"]
        self.assertTrue(candidate["annual_report_ready"])
        self.assertEqual(candidate["review_id"], "review:1")


class CockpitNamesTests(unittest.TestCase):
    def test_members_are_named_from_the_mission_feed_plan(self) -> None:
        from dalton_core.cockpit_plane import CockpitPlane

        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        state = Path(temp.name)
        (state / "feed-plans").mkdir()
        (state / "feed-plans" / "mission-feeds-v1.json").write_text(json.dumps({
            "companies": {WDGT: {"names": ["Widget Robotics Inc", "WDGT"]}}}))
        plane = object.__new__(CockpitPlane)
        plane.state_dir = state
        members = plane._members(MISSION)
        self.assertEqual(members[WDGT]["name"], "Widget Robotics Inc")
        self.assertEqual(CockpitPlane._label(members, WDGT), "WDGT · Widget Robotics Inc")
        # The legacy table still names the legacy universe.
        self.assertEqual(members["company:sec-cik:0000051143"]["name"], "IBM")
        self.assertNotIn("name", members["company:ticker:nocik"])
        block = "\n".join(CockpitPlane._mission_block(
            {"title": "t", "objective": "o", "research_questions": [], "source_plan": []},
            members))
        self.assertIn("WDGT (Widget Robotics Inc)", block)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
