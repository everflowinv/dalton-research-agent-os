"""Search completion must not turn a queued original into a negative result."""
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dalton_core import material_acquisition as ma


class PendingOriginalTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.state = Path(self.temp.name)
        self.db = sqlite3.connect(self.state / "core.sqlite")
        self.addCleanup(self.db.close)
        self.db.executescript("""
            CREATE TABLE coverage_mission_discovery_dispatches (
                dispatch_id TEXT, status TEXT, company_ref TEXT, source_ref TEXT,
                spec_ref TEXT, query_hash TEXT, created_at TEXT, updated_at TEXT);
            CREATE TABLE coverage_mission_source_discoveries (
                record_id TEXT, company_ref TEXT, source_ref TEXT, spec_ref TEXT,
                query_hash TEXT, created_at TEXT);
            CREATE TABLE coverage_mission_discovered_documents (
                discovery_ref TEXT, status TEXT);
        """)
        self.db.execute("INSERT INTO coverage_mission_discovery_dispatches VALUES "
            "(?,?,?,?,?,?,?,?)", ("dispatch:1", "succeeded", "company:test-a",
            "source:sec-edgar", "quarterly-report-10q", "query:1",
            "2026-09-08T00:00:00+00:00", "2026-09-08T00:01:00+00:00"))
        self.db.execute("INSERT INTO coverage_mission_source_discoveries VALUES "
            "(?,?,?,?,?,?)", ("discovery:1", "company:test-a", "source:sec-edgar",
            "quarterly-report-10q", "query:1", "2026-09-08T00:01:00+00:00"))
        self.db.commit()

    def settled(self):
        return ma._dispatch_settled(self.state, {"dispatch_ref": "dispatch:1"},
                                   now="2026-09-08T08:00:00+00:00")

    def test_discovered_and_running_originals_remain_pending_after_search_grace(self):
        for status in ("discovered", "acquisition_launched"):
            self.db.execute("DELETE FROM coverage_mission_discovered_documents")
            self.db.execute("INSERT INTO coverage_mission_discovered_documents VALUES (?,?)",
                            ("discovery:1", status))
            self.db.commit()
            self.assertFalse(self.settled())
        self.db.execute("UPDATE coverage_mission_discovered_documents SET status='acquired'")
        self.db.commit()
        self.assertTrue(self.settled())

    def test_failed_or_other_query_originals_do_not_hold_this_request(self):
        self.db.execute("INSERT INTO coverage_mission_discovered_documents VALUES (?,?)",
                        ("discovery:1", "failed"))
        self.db.commit()
        self.assertTrue(self.settled())
        self.db.execute("UPDATE coverage_mission_discovered_documents SET status='discovered'")
        self.db.execute("UPDATE coverage_mission_source_discoveries SET query_hash='query:other'")
        self.db.commit()
        self.assertTrue(self.settled())

    def test_pending_download_waits_until_request_deadline_without_dispatch_or_spend(self):
        previous = {"created_at": "2026-09-08T00:00:00+00:00",
                    "query": {"targeted_query": "query:1"}}
        request = {"deadline": "2026-09-09T00:00:00+00:00"}
        verdict = {"met": False, "partial": False, "items": [], "reason": "not yet acquired"}
        rung = ma.TargetedSourceRung("sec_edgar")
        with patch.object(ma, "targeted_query_status", return_value={"status": "succeeded"}), \
             patch.object(ma, "evaluate_acceptance", return_value=verdict), \
             patch.object(ma, "_dispatch_settled", return_value=False), \
             patch.object(ma, "enqueue_targeted_query") as dispatch:
            before = ma.RungContext(self.state, request, {}, [],
                                   "2026-09-08T08:00:00+00:00", previous)
            result = rung.settle(before)
            self.assertEqual(result.outcome, "waiting")
            self.assertEqual((result.calls, result.cost_micros), (0, 0))
            self.assertIn("原文", result.detail)
            after = ma.RungContext(self.state, request, {}, [],
                                  "2026-09-09T01:00:00+00:00", previous)
            self.assertEqual(rung.settle(after).outcome, "empty")
            verdict["met"] = True
            self.assertEqual(rung.settle(before).outcome, "accepted")
            dispatch.assert_not_called()
