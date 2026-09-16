"""WP-B2-5: a backup must not be the thing that fills the volume.

The daily pass copied core + scheduler + model-router (about 1.7 GB) with no
idea how much room was left, kept three snapshots, and pruned only *after*
writing the fourth -- so the one moment it needed the most space was the one
moment it had the least. On 2026-09-16 the volume reached 99% with 3.6 GB free.
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from dalton_core.backup import (
    BackupError,
    BackupInsufficientSpace,
    DatabaseBackupManager,
)


def _make_db(path: Path, rows: int = 500) -> None:
    connection = sqlite3.connect(path)
    try:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("CREATE TABLE t(a TEXT)")
        connection.executemany("INSERT INTO t VALUES(?)",
                               [(f"row-{index}" * 20,) for index in range(rows)])
        connection.commit()
    finally:
        connection.close()


class DiskPreflightTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.db = self.root / "core.sqlite"
        _make_db(self.db)
        self.backups = self.root / "backups"

    def manager(self, **kwargs):
        return DatabaseBackupManager(self.backups, {"core": self.db}, **kwargs)

    def test_a_snapshot_is_written_when_there_is_room(self):
        manifest = self.manager().snapshot("20260916T000000.000000Z")
        self.assertEqual(manifest["status"], "fresh")
        self.assertEqual(len(manifest["files"]), 1)

    def test_a_snapshot_is_refused_when_the_volume_is_nearly_full(self):
        manager = self.manager(free_space_multiple=1.0)

        class _Usage:
            free = 1

        def usage(_path):
            return _Usage

        import dalton_core.backup as backup_module
        original = backup_module.shutil.disk_usage
        backup_module.shutil.disk_usage = usage
        try:
            with self.assertRaises(BackupInsufficientSpace) as caught:
                manager.snapshot("20260916T000001.000000Z")
        finally:
            backup_module.shutil.disk_usage = original
        self.assertIn("磁盘余量不足", str(caught.exception))
        self.assertFalse(list(self.backups.glob("2026*")),
                         "a refused snapshot must not leave bytes behind")

    def test_the_estimate_uses_the_larger_of_live_and_previous(self):
        manager = self.manager()
        self.assertGreater(manager.estimate_snapshot_bytes(), 0)
        manager.snapshot("20260916T000002.000000Z")
        self.assertGreater(manager.estimate_snapshot_bytes(), 0)

    def test_retention_runs_before_the_new_snapshot_is_written(self):
        manager = self.manager(keep_latest=2)
        for index in range(4):
            manager.snapshot(f"20260916T00000{index}.000000Z")
        kept = sorted(path.name for path in self.backups.iterdir()
                      if not path.name.startswith("."))
        # Pruning to keep_latest - 1 first, then writing, peaks at keep_latest
        # rather than keep_latest + 1 -- one whole snapshot of headroom.
        self.assertEqual(len(kept), 2, kept)
        self.assertEqual(kept[-1], "20260916T000003.000000Z")

    def test_at_least_one_verified_snapshot_survives_the_prune(self):
        manager = self.manager(keep_latest=1)
        manager.snapshot("20260916T000100.000000Z")
        manager.snapshot("20260916T000101.000000Z")
        kept = sorted(path.name for path in self.backups.iterdir()
                      if not path.name.startswith("."))
        self.assertEqual(kept, ["20260916T000100.000000Z", "20260916T000101.000000Z"])

    def test_the_copy_is_consistent_and_carries_no_wal_sidecar(self):
        # A live WAL is deliberately left hot before the snapshot.
        connection = sqlite3.connect(self.db)
        try:
            connection.execute("INSERT INTO t VALUES('uncheckpointed')")
            connection.commit()
            manifest = self.manager().snapshot("20260916T000009.000000Z")
        finally:
            connection.close()
        copied = self.backups / manifest["snapshot_id"] / manifest["files"][0]["file"]
        self.assertTrue(copied.is_file())
        self.assertFalse(Path(f"{copied}-wal").exists())
        reader = sqlite3.connect(f"file:{copied}?mode=ro", uri=True)
        try:
            self.assertEqual(
                reader.execute("SELECT count(*) FROM t WHERE a='uncheckpointed'"
                               ).fetchone()[0], 1,
                "the copy must include the committed WAL frames")
            self.assertEqual(
                reader.execute("PRAGMA integrity_check").fetchone()[0], "ok")
        finally:
            reader.close()

    def test_bad_bounds_are_refused_at_construction(self):
        with self.assertRaises(BackupError):
            self.manager(keep_latest=0)
        with self.assertRaises(BackupError):
            self.manager(free_space_multiple=0.5)

    def test_verify_restore_still_reproduces_the_bytes(self):
        manifest = self.manager().snapshot("20260916T000010.000000Z")
        result = self.manager().verify_restore(
            manifest["snapshot_id"], self.root / "restore")
        self.assertEqual(result["status"], "verified")


if __name__ == "__main__":
    unittest.main()
