import json
import sqlite3
import unittest

from dalton_core.model_coverage_status import model_coverage_status
from dalton_core.needs_human import completion_entry, model_coverage_gaps


class CoverageStatusTests(unittest.TestCase):
    def setUp(self):
        self.core = sqlite3.connect(":memory:")
        self.addCleanup(self.core.close)
        self.core.executescript("""
            CREATE TABLE coverage_mission_versions(mission_version_id TEXT,record_json TEXT);
            CREATE TABLE coverage_mission_pointer(mission_ref TEXT,mission_version_id TEXT,updated_at TEXT);
            CREATE TABLE forecast_model_versions(record_json TEXT,created_at TEXT,version_number INTEGER);
            CREATE TABLE coverage_mission_company_model_specs(company_ref TEXT);
            CREATE TABLE coverage_mission_statement_filings(company_ref TEXT);
        """)
        self.mission("old", ["OLD"])
        self.core.execute("DELETE FROM coverage_mission_pointer")
        self.mission("current", ["ALPHA", "BETA", "GAMMA"])

    def mission(self, ref, tickers):
        body = {"universe": [{"company_ref": "company:" + t, "ticker": t} for t in tickers],
                "autonomy": {"may_write": ["forecast_line"]}}
        self.core.execute("INSERT INTO coverage_mission_versions VALUES(?,?)", (ref,json.dumps(body)))
        self.core.execute("INSERT INTO coverage_mission_pointer VALUES(?,?,?)", ("mission:synthetic",ref,"2026-10-09"))

    def test_current_coverage_and_exact_missing_prerequisites(self):
        self.core.execute("INSERT INTO coverage_mission_company_model_specs VALUES('company:BETA')")
        self.core.execute("INSERT INTO coverage_mission_statement_filings VALUES('company:BETA')")
        self.core.execute("INSERT INTO forecast_model_versions VALUES(?,?,?)", (
            json.dumps({"company_ref":"company:GAMMA","id":"model:gamma:v1","created_at":"2026-10-09"}),"2026-10-09",1))
        before = self.core.total_changes
        self.core.execute("PRAGMA query_only=ON")
        statuses = model_coverage_status(self.core)
        self.assertEqual(set(statuses), {"company:ALPHA", "company:BETA", "company:GAMMA"})
        self.assertEqual([r["code"] for r in statuses["company:ALPHA"]["reasons"]],
                         ["missing_filed_statements", "missing_model_spec"])
        self.assertEqual(statuses["company:BETA"]["reasons"][0]["code"], "forecast_not_published")
        self.assertEqual(statuses["company:GAMMA"]["model_version_ref"], "model:gamma:v1")
        cards = model_coverage_gaps(self.core)
        self.assertEqual(len(cards), 2)
        self.assertTrue(all(not c["actionable"] for c in cards))
        self.assertEqual(completion_entry(cards[0])["mode"], "company")
        self.assertEqual(self.core.total_changes, before)

    def test_reminder_disappears_after_model_publication(self):
        self.assertEqual(len(model_coverage_gaps(self.core)), 3)
        self.core.execute("INSERT INTO forecast_model_versions VALUES(?,?,?)", (
            json.dumps({"company_ref":"company:ALPHA","id":"model:alpha:v1"}),"2026-10-09",1))
        self.assertEqual(len(model_coverage_gaps(self.core)), 2)

    def test_no_schema_is_an_empty_read(self):
        with sqlite3.connect(":memory:") as empty:
            self.assertEqual(model_coverage_status(empty), {})


if __name__ == "__main__":
    unittest.main()
