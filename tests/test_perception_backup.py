from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from dalton_core.backup import BackupError, DatabaseBackupManager
from dalton_core.perception import LegacyCoveragePerceptionAdapter, PerceptionError, validate_snapshot


class PerceptionBackupTests(unittest.TestCase):
    def legacy(self, path: Path) -> None:
        conn = sqlite3.connect(path)
        conn.executescript("""
        CREATE TABLE companies(slug TEXT PRIMARY KEY,name TEXT,ticker TEXT,market TEXT,coverage_tier TEXT,coverage_status TEXT,archetype TEXT,investment_view TEXT,updated_at TEXT);
        CREATE TABLE events(id INTEGER,event_key TEXT,company_slug TEXT,event_type TEXT,occurred_at TEXT,title TEXT,summary TEXT,materiality TEXT,status TEXT,source_url TEXT,updated_at TEXT);
        CREATE TABLE evidence(id INTEGER,evidence_key TEXT,company_slug TEXT,claim TEXT,stance TEXT,source TEXT,source_url TEXT,as_of TEXT,confidence TEXT,valid_until TEXT,created_at TEXT);
        CREATE TABLE filings(id INTEGER,company_slug TEXT,form TEXT,filing_date TEXT,report_date TEXT,accession_no TEXT,created_at TEXT);
        INSERT INTO companies VALUES('wanhua','万华化学','600309.SS','CN','A','active','chemical','under review','2026-08-14T00:00:00+00:00');
        INSERT INTO events VALUES(1,'event-1','wanhua','filing','2026-08-14T00:00:00+00:00','New filing','summary','high','new','https://example.com','2026-08-14T00:00:00+00:00');
        INSERT INTO evidence VALUES(1,'evidence-1','wanhua','claim','supports','filing','https://example.com','2026-08-14','high','2026-09-14','2026-08-14T00:00:00+00:00');
        INSERT INTO filings VALUES(1,'wanhua','10-Q','2026-08-14','2026-06-30','0001','2026-08-14T00:00:00+00:00');
        """)
        conn.commit()
        conn.close()

    def test_perception_uses_closed_snapshot_contract(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "coverage.sqlite"
            self.legacy(source)
            output = root / "snapshot.json"
            snapshot = LegacyCoveragePerceptionAdapter(source).write("wanhua", output)
            self.assertEqual(snapshot["company"]["slug"], "wanhua")
            self.assertEqual(len(snapshot["evidence"]), 1)
            self.assertEqual(validate_snapshot(json.loads(output.read_text()))["content_hash"], snapshot["content_hash"])
            self.assertEqual(output.stat().st_mode & 0o777, 0o600)
            broken = dict(snapshot)
            broken["company"] = dict(broken["company"], name="changed")
            with self.assertRaises(PerceptionError):
                validate_snapshot(broken)

    def test_schema_drift_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "bad.sqlite"
            sqlite3.connect(source).execute("CREATE TABLE companies(slug TEXT)").connection.close()
            with self.assertRaises(PerceptionError):
                LegacyCoveragePerceptionAdapter(source).build("wanhua")

    def test_backup_and_restore_are_hash_and_integrity_checked(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "core.sqlite"
            conn = sqlite3.connect(database)
            conn.execute("CREATE TABLE values_table(value TEXT)")
            conn.execute("INSERT INTO values_table VALUES('authority')")
            conn.commit()
            conn.close()
            manager = DatabaseBackupManager(root / "backups", {"core": database})
            manifest = manager.snapshot("snapshot-1")
            self.assertEqual(manifest["status"], "fresh")
            restored = manager.verify_restore("snapshot-1", root / "restore")
            self.assertEqual(restored["status"], "verified")
            restored_db = root / "restore" / "core.sqlite"
            self.assertEqual(sqlite3.connect(restored_db).execute("SELECT value FROM values_table").fetchone()[0], "authority")
            with self.assertRaises(BackupError):
                manager.verify_restore("snapshot-1", root / "restore")


if __name__ == "__main__":
    unittest.main()
