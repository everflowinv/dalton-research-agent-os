from __future__ import annotations

import json
import hashlib
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest import mock

from dalton_core.backup import BackupError, DatabaseBackupManager
from dalton_core.perception import LegacyCoveragePerceptionAdapter, PerceptionError, validate_snapshot


class PerceptionBackupTests(unittest.TestCase):
    def add_live_wal_sidecars(self, snapshot: Path) -> None:
        database = snapshot / "core.sqlite"
        connection = sqlite3.connect(database)
        self.assertEqual(connection.execute("PRAGMA journal_mode=WAL").fetchone()[0], "wal")
        connection.execute("CREATE TABLE IF NOT EXISTS wal_fixture(value INTEGER)")
        connection.execute("INSERT INTO wal_fixture VALUES(1)")
        connection.commit()
        connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        wal_bytes = Path(f"{database}-wal").read_bytes()
        shm_bytes = Path(f"{database}-shm").read_bytes()
        connection.close()
        Path(f"{database}-wal").write_bytes(wal_bytes)
        Path(f"{database}-shm").write_bytes(shm_bytes)
        Path(f"{database}-wal").chmod(0o600)
        Path(f"{database}-shm").chmod(0o600)
        manifest_path = snapshot / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["files"][0]["size_bytes"] = database.stat().st_size
        manifest["files"][0]["sha256"] = hashlib.sha256(database.read_bytes()).hexdigest()
        manifest_path.write_text(json.dumps(
            manifest, sort_keys=True, separators=(",", ":")) + "\n", encoding="utf-8")
        manifest_path.chmod(0o600)

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

    def test_retention_keeps_three_verified_snapshots_and_ignores_unknown_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "core.sqlite"
            connection = sqlite3.connect(database)
            connection.execute("CREATE TABLE authority(value INTEGER)")
            connection.commit()
            manager = DatabaseBackupManager(root / "backups", {"core": database})
            for index in range(5):
                connection.execute("INSERT INTO authority VALUES(?)", (index,))
                connection.commit()
                manager.snapshot(f"snapshot-{index}")
            connection.close()
            incomplete = root / "backups" / ".snapshot-writing.tmp"
            incomplete.mkdir()
            unknown = root / "backups" / "operator-notes"
            unknown.mkdir()
            (unknown / "README").write_text("not a completed snapshot", encoding="utf-8")

            result = manager.prune_verified(keep_latest=3)

            self.assertEqual(result["status"], "pruned")
            self.assertEqual(result["retained_snapshot_ids"],
                             ["snapshot-4", "snapshot-3", "snapshot-2"])
            self.assertEqual(result["deleted_snapshot_ids"], ["snapshot-0", "snapshot-1"])
            self.assertEqual(result["deleted_count"], 2)
            self.assertGreater(result["deleted_bytes"], 0)
            self.assertTrue(all(item["hashes_verified"] and item["sqlite_integrity"] == "ok"
                                for item in result["retained_checks"]))
            self.assertTrue(incomplete.is_dir())
            self.assertTrue(unknown.is_dir())
            self.assertEqual({item["snapshot_id"] for item in result["skipped"]},
                             {incomplete.name, unknown.name})

    def test_retention_does_not_delete_when_three_good_snapshots_cannot_be_proved(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "core.sqlite"
            connection = sqlite3.connect(database)
            connection.execute("CREATE TABLE authority(value INTEGER)")
            connection.commit()
            manager = DatabaseBackupManager(root / "backups", {"core": database})
            for index in range(3):
                connection.execute("INSERT INTO authority VALUES(?)", (index,))
                connection.commit()
                manager.snapshot(f"snapshot-{index}")
            connection.close()
            (root / "backups" / "snapshot-2" / "core.sqlite").write_bytes(b"corrupt")

            result = manager.prune_verified(keep_latest=3)

            self.assertEqual(result["status"], "held")
            self.assertEqual(result["deleted_count"], 0)
            self.assertEqual({path.name for path in (root / "backups").iterdir()},
                             {"snapshot-0", "snapshot-1", "snapshot-2"})
            self.assertIn("fewer than keep_latest", result["reason"])

    def test_real_wal_snapshot_with_empty_wal_and_normal_shm_verifies_end_to_end(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "core.sqlite"
            connection = sqlite3.connect(database)
            connection.execute("CREATE TABLE authority(value INTEGER)")
            connection.commit()
            connection.close()
            manager = DatabaseBackupManager(root / "backups", {"core": database})
            manager.snapshot("snapshot-wal")
            snapshot = root / "backups" / "snapshot-wal"
            self.add_live_wal_sidecars(snapshot)
            self.assertEqual((snapshot / "core.sqlite-wal").stat().st_size, 0)
            self.assertGreaterEqual((snapshot / "core.sqlite-shm").stat().st_size, 32768)

            restored = manager.verify_restore("snapshot-wal", root / "restore-wal")
            self.assertEqual(restored["status"], "verified")
            held = manager.prune_verified(keep_latest=1)
            self.assertEqual(held["retained_snapshot_ids"], ["snapshot-wal"])
            self.assertEqual(held["retained_checks"][0]["sqlite_integrity"], "ok")
            (snapshot / "core.sqlite-wal").write_bytes(b"not empty")
            refused = manager.prune_verified(keep_latest=1)
            self.assertEqual(refused["status"], "held")
            self.assertEqual(refused["deleted_count"], 0)
            self.assertIn("WAL must be", refused["skipped"][0]["reason"])

    def test_snapshot_being_restored_is_not_deleted(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "core.sqlite"
            connection = sqlite3.connect(database)
            connection.execute("CREATE TABLE authority(value INTEGER)")
            connection.commit()
            manager = DatabaseBackupManager(root / "backups", {"core": database})
            for index in range(4):
                connection.execute("INSERT INTO authority VALUES(?)", (index,))
                connection.commit()
                manager.snapshot(f"snapshot-{index}")
            connection.close()
            entered, release = threading.Event(), threading.Event()
            actual_copy = __import__("shutil").copyfile

            def blocked_copy(source, destination):
                entered.set()
                release.wait(5)
                return actual_copy(source, destination)

            outcome = []
            with mock.patch("dalton_core.backup.shutil.copyfile", blocked_copy):
                thread = threading.Thread(target=lambda: outcome.append(
                    manager.verify_restore("snapshot-0", root / "restore")))
                thread.start()
                self.assertTrue(entered.wait(2))
                result = manager.prune_verified(keep_latest=3)
                release.set()
                thread.join(5)
            self.assertEqual(outcome[0]["status"], "verified")
            self.assertTrue((root / "backups" / "snapshot-0").is_dir())
            self.assertEqual(result["deleted_count"], 0)
            self.assertIn("in use", next(item["reason"] for item in result["skipped"]
                                         if item["snapshot_id"] == "snapshot-0"))

    def test_new_unmanifested_file_at_delete_boundary_holds_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "core.sqlite"
            connection = sqlite3.connect(database)
            connection.execute("CREATE TABLE authority(value INTEGER)")
            connection.commit()
            manager = DatabaseBackupManager(root / "backups", {"core": database})
            for index in range(4):
                connection.execute("INSERT INTO authority VALUES(?)", (index,))
                connection.commit()
                manager.snapshot(f"snapshot-{index}")
            connection.close()

            def add_file(snapshot):
                (snapshot / "appeared-after-inventory").write_text("hold", encoding="utf-8")
                return []

            with mock.patch("dalton_core.backup._external_open_pids", side_effect=add_file):
                result = manager.prune_verified(keep_latest=3)
            self.assertEqual(result["deleted_count"], 0)
            self.assertTrue((root / "backups" / "snapshot-0").is_dir())
            self.assertIn("changed before deletion", result["skipped"][-1]["reason"])

    @unittest.skipUnless(shutil.which("lsof"), "platform has no open-handle probe")
    def test_external_reader_without_advisory_lock_is_detected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            database = root / "core.sqlite"
            connection = sqlite3.connect(database)
            connection.execute("CREATE TABLE authority(value INTEGER)")
            connection.commit()
            manager = DatabaseBackupManager(root / "backups", {"core": database})
            for index in range(4):
                connection.execute("INSERT INTO authority VALUES(?)", (index,))
                connection.commit()
                manager.snapshot(f"snapshot-{index}")
            connection.close()
            manifest = root / "backups" / "snapshot-0" / "manifest.json"
            reader = subprocess.Popen(
                [sys.executable, "-c",
                 "import sys; f=open(sys.argv[1], 'rb'); print('ready', flush=True); input()",
                 str(manifest)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                text=True,
            )
            try:
                self.assertEqual(reader.stdout.readline().strip(), "ready")
                result = manager.prune_verified(keep_latest=3)
            finally:
                reader.communicate("\n", timeout=5)
            self.assertEqual(result["deleted_count"], 0)
            self.assertTrue(manifest.is_file())
            reason = next(item["reason"] for item in result["skipped"]
                          if item["snapshot_id"] == "snapshot-0")
            self.assertIn(f"pid(s) {reader.pid}", reason)


if __name__ == "__main__":
    unittest.main()
