from __future__ import annotations

import hashlib
import json
import sqlite3
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path

from dalton_core.legacy_migration import migrate_legacy_workspace


class LegacyMigrationTests(unittest.TestCase):
    def test_shadow_import_is_lossless_quarantined_and_non_mutating(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "workspace-chem"
            state = root / "state"
            (source / "research-outputs").mkdir(parents=True)
            (source / "temp").mkdir()
            (source / "data" / "coverage").mkdir(parents=True)
            (source / "data" / "prices").mkdir(parents=True)
            (source / "AGENTS.md").write_text("legacy constraints\n", encoding="utf-8")
            (source / "research-outputs" / "report.md").write_text(
                "legacy report\n", encoding="utf-8"
            )
            (source / "research-outputs" / "report-copy.md").write_text(
                "legacy report\n", encoding="utf-8"
            )
            (source / "temp" / "scratch.txt").write_text("exclude\n", encoding="utf-8")
            (source / "data" / "prices" / "prices.csv").write_text(
                "date,price\n", encoding="utf-8"
            )
            database = source / "data" / "coverage" / "coverage.db"
            with sqlite3.connect(database) as connection:
                connection.executescript(
                    """
                    CREATE TABLE theses(id INTEGER, company_slug TEXT, thesis_key TEXT, status TEXT);
                    INSERT INTO theses VALUES(1, 'wanhua', 'WH-T1', 'open');
                    CREATE TABLE tasks(id INTEGER, task_key TEXT, company_slug TEXT, status TEXT, due_at TEXT);
                    INSERT INTO tasks VALUES(1, 'task-1', 'wanhua', 'ready', '2026-08-19T00:00:00Z');
                    CREATE TABLE companies(slug TEXT);
                    INSERT INTO companies VALUES('wanhua');
                    """
                )
            source_hash = hashlib.sha256(database.read_bytes()).hexdigest()
            cron_path = root / "crons.json"
            cron_path.write_text(
                json.dumps(
                    {
                        "jobs": [
                            {
                                "id": "dalton-1",
                                "name": "dalton-coverage-dispatcher",
                                "agentId": "chem",
                                "enabled": True,
                                "schedule": {"kind": "cron", "expr": "5 * * * *"},
                                "payload": {"kind": "command"},
                            },
                            {
                                "id": "other-1",
                                "name": "other",
                                "agentId": "main",
                                "enabled": True,
                            },
                        ]
                    }
                ),
                encoding="utf-8",
            )
            result = migrate_legacy_workspace(
                source,
                state,
                cron_snapshot=cron_path,
                created_at=datetime(2026, 8, 14, 8, 0, tzinfo=timezone.utc),
            )

            self.assertEqual(result["mode"], "shadow")
            self.assertEqual(result["source_live_status"], "unchanged")
            self.assertEqual(result["schedule_count"], 1)
            self.assertEqual(
                result["schedules"][0]["migration_status"],
                "shadow_registered_not_scheduled",
            )
            self.assertEqual(
                result["legacy_authority"]["theses"][0]["migration_status"],
                "quarantined_pending_core_verification",
            )
            self.assertNotIn("temp/scratch.txt", {item["source_path"] for item in result["artifacts"]})
            self.assertEqual(hashlib.sha256(database.read_bytes()).hexdigest(), source_hash)
            self.assertTrue(Path(result["manifest_path"]).is_file())
            self.assertTrue((state / "core.sqlite").is_file())
            with sqlite3.connect(state / "core.sqlite") as connection:
                count = connection.execute(
                    "SELECT COUNT(*) FROM observability_artifact_versions"
                ).fetchone()[0]
            self.assertEqual(count, result["artifact_count"])
            for artifact in result["artifacts"]:
                stored = Path(artifact["storage_path"])
                self.assertEqual(hashlib.sha256(stored.read_bytes()).hexdigest(), artifact["sha256"])


if __name__ == "__main__":
    unittest.main()
