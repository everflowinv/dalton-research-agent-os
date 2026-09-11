from __future__ import annotations

import hashlib
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path

from scripts.configure_backup_retention_stopped_window import (
    RetentionConfigError,
    canonical_sha256,
    install,
)


class BackupRetentionStoppedWindowTests(unittest.TestCase):
    def setUp(self) -> None:
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.config = self.root / "service.json"
        self.receipt = self.root / "receipt.json"
        self.raw = {
            "schema_version": "0.1",
            "core_db": "/state/core.sqlite",
            "plugins": ["existing"],
            "backup": {
                "enabled": True,
                "root": "/state/backups",
                "interval_seconds": 86400,
            },
        }
        self.config.write_text(json.dumps(self.raw, indent=2) + "\n", encoding="utf-8")
        os.chmod(self.config, 0o640)

    def _sha(self) -> str:
        return hashlib.sha256(self.config.read_bytes()).hexdigest()

    def test_installs_only_keep_latest_three_and_writes_a_pending_receipt(self) -> None:
        before = self._sha()
        receipt = install(self.config, before, self.receipt, config_validator=lambda _raw: None)
        after = json.loads(self.config.read_text())
        expected = json.loads(json.dumps(self.raw))
        expected["backup"]["keep_latest"] = 3
        self.assertEqual(after, expected)
        self.assertEqual(stat.S_IMODE(self.config.stat().st_mode), 0o640)
        self.assertEqual(receipt["delta"], {"backup.keep_latest": 3})
        self.assertEqual(receipt["status"], "configured_controller_start_pending")
        self.assertEqual(receipt["service_lifecycle_mutations"], 0)
        self.assertFalse(receipt["release_manifest_modified"])
        unsigned = dict(receipt)
        content_hash = unsigned.pop("content_hash")
        self.assertEqual(content_hash, canonical_sha256(unsigned))
        self.assertEqual(json.loads(self.receipt.read_text()), receipt)

    def test_wrong_precondition_preserves_config_and_creates_no_receipt(self) -> None:
        before = self.config.read_bytes()
        with self.assertRaisesRegex(RetentionConfigError, "reviewed precondition"):
            install(self.config, "0" * 64, self.receipt, config_validator=lambda _raw: None)
        self.assertEqual(self.config.read_bytes(), before)
        self.assertFalse(self.receipt.exists())

    def test_existing_owner_receipt_stops_before_config_mutation(self) -> None:
        before = self.config.read_bytes()
        self.receipt.write_text("owner\n")
        with self.assertRaisesRegex(RetentionConfigError, "already exists"):
            install(self.config, self._sha(), self.receipt, config_validator=lambda _raw: None)
        self.assertEqual(self.config.read_bytes(), before)
        self.assertEqual(self.receipt.read_text(), "owner\n")

    def test_disabled_backup_cannot_gain_retention(self) -> None:
        self.raw["backup"]["enabled"] = False
        self.config.write_text(json.dumps(self.raw), encoding="utf-8")
        with self.assertRaisesRegex(RetentionConfigError, "enabled backup"):
            install(self.config, self._sha(), self.receipt, config_validator=lambda _raw: None)

    def test_existing_keep_latest_is_the_only_replaced_value(self) -> None:
        self.raw["backup"]["keep_latest"] = 9
        self.config.write_text(json.dumps(self.raw), encoding="utf-8")
        receipt = install(self.config, self._sha(), self.receipt, config_validator=lambda _raw: None)
        self.assertEqual(json.loads(self.config.read_text())["backup"]["keep_latest"], 3)
        preserved = json.loads(json.dumps(self.raw))
        preserved["backup"].pop("keep_latest")
        self.assertEqual(
            receipt["preserved_without_backup_keep_latest_sha256"],
            canonical_sha256(preserved),
        )

    def test_new_runtime_validator_runs_before_any_config_write(self) -> None:
        before = self.config.read_bytes()

        def reject(_raw):
            raise RetentionConfigError("runtime rejected")

        with self.assertRaisesRegex(RetentionConfigError, "runtime rejected"):
            install(self.config, self._sha(), self.receipt, config_validator=reject)
        self.assertEqual(self.config.read_bytes(), before)
        self.assertFalse(self.receipt.exists())

    def test_concurrent_owner_change_wins_and_cas_refuses_to_replace_it(self) -> None:
        def owner_changes_config(_raw):
            changed = json.loads(json.dumps(self.raw))
            changed["plugins"].append("new-owner-value")
            self.config.write_text(json.dumps(changed, indent=2) + "\n")

        with self.assertRaisesRegex(RetentionConfigError, "changed during"):
            install(self.config, self._sha(), self.receipt, config_validator=owner_changes_config)
        self.assertEqual(json.loads(self.config.read_text())["plugins"],
                         ["existing", "new-owner-value"])
        self.assertFalse(self.receipt.exists())


if __name__ == "__main__":
    unittest.main()
