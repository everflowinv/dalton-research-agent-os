from __future__ import annotations

import json
import os
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from scripts import openclaw_broker_stopped_window as stopped
from tests.test_successor_config_transition import PreserveExistingTransitionTests


class OpenClawBrokerStoppedWindowTests(PreserveExistingTransitionTests):
    def setUp(self) -> None:
        super().setUp()
        self.manifest = self.build_external()
        self.config = self.root / "live-openclaw.json"
        self.config.write_bytes((self.packet / "openclaw.before.json").read_bytes())
        self.journal = self.packet / "broker.journal.json"
        self.receipts = self.root / "broker-receipt"
        self.openclaw_root = self.root / "openclaw/dist"
        self.openclaw_root.mkdir(parents=True)

    def _apply(self, *, identities=None, active_children: bool = False):
        sequence = iter(identities or [
            {"pid": 10, "started_at": "1970-01-01T00:00:00.100Z",
             "started_at_ms": 100},
            {"pid": 11, "started_at": "1970-01-01T00:00:00.200Z",
             "started_at_ms": 200},
            {"pid": 12, "started_at": "1970-01-01T00:00:00.300Z",
             "started_at_ms": 300},
        ])
        gateway = {"loaded": True}

        def stop(*_args, **_kwargs): gateway["loaded"] = False
        def start(*_args, **_kwargs):
            gateway["loaded"] = True
            return next(sequence)

        def children(*_args, **_kwargs):
            if active_children:
                raise stopped.BrokerStoppedWindowError(
                    "Dalton launch drain has 1 active child ticket(s)")
        with patch.object(stopped, "_source_identity"), \
             patch.object(stopped, "_children_clear", side_effect=children), \
             patch.object(stopped, "loaded",
                          side_effect=lambda label, **_k:
                              gateway["loaded"]
                              if label == stopped.GATEWAY_LABEL else False), \
             patch.object(stopped, "gateway_identity",
                          side_effect=lambda **_k: next(sequence)), \
             patch.object(stopped, "stop_gateway", side_effect=stop), \
             patch.object(stopped, "start_gateway", side_effect=start):
            result = stopped.apply_reviewed_transition(
                packet_root=self.packet, transition=self.manifest,
                source_root=self.root, config_path=self.config,
                openclaw_root=self.openclaw_root, state_dir=self.state,
                journal_path=self.journal, receipt_dir=self.receipts,
            )
        return result

    def test_exact_historical_pending_survives_one_stop_start(self):
        result = self._apply()
        self.assertEqual("applied_gateway_restarted", result["status"])
        self.assertEqual(1, result["historical_unresolved_count"])
        self.assertFalse(result["retry_authorized"])
        self.assertFalse(result["refund_authorized"])
        self.assertEqual((self.packet / "openclaw.after.json").read_bytes(),
                         self.config.read_bytes())
        self.assertEqual(json.loads(self.journal.read_text())["records"],
                         self.manifest["external_config_transitions"][0]
                         ["historical_unresolved"]["records"])
        self.assertTrue((self.receipts / "receipt.json").is_file())

    def test_active_owned_child_refuses_before_config_or_gateway_mutation(self):
        before = self.config.read_bytes()
        with self.assertRaisesRegex(stopped.BrokerStoppedWindowError,
                                    "active child ticket"):
            self._apply(active_children=True)
        self.assertEqual(before, self.config.read_bytes())
        self.assertFalse(self.receipts.exists())

    def test_journal_change_refuses_before_config_or_gateway_mutation(self):
        value = json.loads(self.journal.read_text())
        value["records"][0]["requestHash"] = "b" * 64
        self.journal.write_text(json.dumps(value, indent=2) + "\n")
        before = self.config.read_bytes()
        with self.assertRaisesRegex(stopped.BrokerStoppedWindowError,
                                    "uncertainty set changed"):
            self._apply()
        self.assertEqual(before, self.config.read_bytes())
        self.assertFalse(self.receipts.exists())

    def test_rollback_restores_exact_before_and_preserves_historical_pending(self):
        self._apply()
        identities = iter([
            {"pid": 12, "started_at": "1970-01-01T00:00:00.300Z",
             "started_at_ms": 300},
            {"pid": 13, "started_at": "1970-01-01T00:00:00.400Z",
             "started_at_ms": 400},
        ])
        gateway = {"loaded": True}
        def stop(*_args, **_kwargs): gateway["loaded"] = False
        def start(*_args, **_kwargs):
            gateway["loaded"] = True
            return next(identities)
        with patch.object(stopped, "_source_identity"), \
             patch.object(stopped, "_children_clear"), \
             patch.object(stopped, "loaded",
                          side_effect=lambda label, **_k:
                              gateway["loaded"]
                              if label == stopped.GATEWAY_LABEL else False), \
             patch.object(stopped, "gateway_identity",
                          side_effect=lambda **_k: next(identities)), \
             patch.object(stopped, "stop_gateway", side_effect=stop), \
             patch.object(stopped, "start_gateway", side_effect=start):
            result = stopped.rollback_reviewed_transition(
                packet_root=self.packet, transition=self.manifest,
                source_root=self.root, config_path=self.config,
                openclaw_root=self.openclaw_root, state_dir=self.state,
                journal_path=self.journal,
                receipt_path=self.receipts / "receipt.json",
            )
        self.assertEqual("rolled_back_gateway_restarted", result["status"])
        self.assertEqual((self.packet / "openclaw.before.json").read_bytes(),
                         self.config.read_bytes())
        self.assertTrue((self.receipts / "rollback.json").is_file())

    def test_owned_cas_never_overwrites_racing_regular_file(self):
        before = self.config.read_bytes()
        after = (self.packet / "openclaw.after.json").read_bytes()
        original_link = os.link
        external = b'{"owner":"racing-writer"}\n'

        def race(source, destination, *args, **kwargs):
            if Path(destination) == self.config:
                self.config.write_bytes(external)
            return original_link(source, destination, *args, **kwargs)

        with patch.object(stopped.os, "link", side_effect=race):
            with self.assertRaises(FileExistsError):
                stopped._compare_and_install(self.config, before, after)
        self.assertEqual(external, self.config.read_bytes())

    def test_owned_cas_preserves_dangling_symlink_installed_before_rename(self):
        before = self.config.read_bytes()
        after = (self.packet / "openclaw.after.json").read_bytes()
        original_rename = os.rename
        destination = self.root / "owner-missing-target"
        injected = False

        def race(source, target, *args, **kwargs):
            nonlocal injected
            if Path(source) == self.config and not injected:
                injected = True
                self.config.unlink()
                self.config.symlink_to(destination)
            return original_rename(source, target, *args, **kwargs)

        with patch.object(stopped.os, "rename", side_effect=race):
            with self.assertRaisesRegex(stopped.BrokerStoppedWindowError,
                                        "changed during owned CAS"):
                stopped._compare_and_install(self.config, before, after)
        self.assertTrue(self.config.is_symlink())
        self.assertEqual(str(destination), os.readlink(self.config))

    def test_owned_cas_fsync_failure_restores_original_inode_and_bytes(self):
        before = self.config.read_bytes()
        before_stat = self.config.stat()
        after = (self.packet / "openclaw.after.json").read_bytes()
        with patch.object(stopped, "_fsync_parent",
                          side_effect=OSError("injected config fsync failure")):
            with self.assertRaisesRegex(OSError, "config fsync failure"):
                stopped._compare_and_install(self.config, before, after)
        restored = self.config.stat()
        self.assertEqual((before_stat.st_dev, before_stat.st_ino),
                         (restored.st_dev, restored.st_ino))
        self.assertEqual(before, self.config.read_bytes())

    def test_receipt_publication_failure_rolls_back_config(self):
        before = self.config.read_bytes()
        with patch.object(stopped, "_write_exclusive",
                          side_effect=OSError("injected receipt failure")):
            with self.assertRaisesRegex(OSError, "injected receipt failure"):
                self._apply()
        self.assertEqual(before, self.config.read_bytes())
        self.assertFalse((self.receipts / "receipt.json").exists())

    def test_exclusive_receipt_cleanup_preserves_racing_owner_path(self):
        path = self.root / "receipt.json"
        owner_bytes = b'{"owner":"external"}\n'
        original_fsync = os.fsync
        calls = 0

        def fail_directory_fsync(descriptor):
            nonlocal calls
            calls += 1
            if calls == 2:
                path.unlink()
                path.write_bytes(owner_bytes)
                raise OSError("injected directory fsync failure")
            return original_fsync(descriptor)

        with patch.object(stopped.os, "fsync", side_effect=fail_directory_fsync):
            with self.assertRaisesRegex(OSError, "directory fsync failure"):
                stopped._write_exclusive(path, {"status": "reviewed"})
        self.assertEqual(owner_bytes, path.read_bytes())

    def test_loaded_plugin_must_match_reviewed_root_version_and_status(self):
        expected = [{
            "plugin_id": "dalton-openclaw-web-search-broker",
            "destination": str(self.root / "managed-plugin"),
            "tree_sha256": "a" * 64,
            "version": "0.1.0-spike.2",
        }]
        wire = {"plugin": {
            "id": "dalton-openclaw-web-search-broker",
            "rootDir": str(self.root / "legacy-plugin"),
            "version": "0.1.0-spike.1",
            "enabled": True, "activated": True, "status": "loaded",
        }}

        def run(*_args, **_kwargs):
            return subprocess.CompletedProcess([], 0, json.dumps(wire), "")

        with self.assertRaisesRegex(stopped.BrokerStoppedWindowError,
                                    "not loaded from reviewed bytes"):
            stopped._verify_loaded_plugins(self.openclaw_root, expected, run=run)


if __name__ == "__main__":
    unittest.main()
