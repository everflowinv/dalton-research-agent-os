from __future__ import annotations

import io
import json
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from scripts import execute_successor_stopped_window_candidate as execute
from scripts.prepare_successor_config_transition import WRITER_APPEND_SCHEMA_VERSION


class WriterRollbackTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.state = self.root / "state"
        self.packet = self.root / "packet"
        self.rollback = self.root / "rollback"
        self.saved = self.rollback / "state-files"
        for path in (self.state, self.packet, self.saved):
            path.mkdir(parents=True)
        self.before = b'{"core":"same-token","operations":["old"]}\n'
        self.after = b'{"core":"same-token","operations":["new","old"]}\n'
        self.target = self.state / "writer-tokens.json"
        for directory, raw in ((self.state, self.after), (self.saved, self.before)):
            (directory / "writer-tokens.json").write_bytes(raw)
            (directory / "writer-tokens.json").chmod(0o600)
            (directory / "owner.json").write_bytes(b'{"owner":"unchanged"}\n')
        self.initial = {"protected_state_sha256": execute.r11.protected_state_hash(self.saved)}
        self.initial_path = self.rollback / "initial-state.json"
        self.initial_path.write_text(json.dumps(self.initial))
        self.initial_bytes = self.initial_path.read_bytes()
        manifest = self.packet / "transition.json"
        manifest.write_text(json.dumps({"schema_version": WRITER_APPEND_SCHEMA_VERSION}))
        self.worker = execute.SuccessorOrchestrator(self.packet, io.StringIO())
        self.worker.rollback_root = self.rollback
        self.worker.artifacts = {"transition_manifest": manifest}
        self.worker.successor_source = self.root
        self.row = {"proof": {"added_operations": ["new"],
                             "predecessor": {"commit": "a" * 40},
                             "successor": {"commit": "b" * 40}}}
        self.addCleanup(patch.stopall)
        patch.object(execute.r11, "STATE", self.state).start()
        patch.object(execute, "expected_writer_operation_transition_state",
                     return_value=(self.before, self.after, self.row)).start()

    def test_installed_append_and_rollback_restore_exact_original(self):
        self.worker._verify_writer_protected_state(self.initial, require_after=True)
        self.worker.verify_rollback_protected_state(self.initial)
        self.worker.restore_rollback_protected_state(self.initial)
        self.assertEqual(self.target.read_bytes(), self.before)
        self.assertEqual(self.target.stat().st_mode & 0o7777, 0o600)
        self.assertEqual(self.initial_path.read_bytes(), self.initial_bytes)
        self.assertEqual(execute.r11.protected_state_hash(self.state),
                         self.initial["protected_state_sha256"])
        self.assertEqual((self.rollback / "writer-token-restore/displaced-current").read_bytes(),
                         self.after)

    def test_before_endpoint_is_valid_for_rollback_but_not_install_success(self):
        self.target.write_bytes(self.before)
        self.worker.restore_rollback_protected_state(self.initial)
        with self.assertRaisesRegex(execute.SuccessorExecuteError, "endpoints"):
            self.worker._verify_writer_protected_state(self.initial, require_after=True)
        self.assertFalse((self.rollback / "writer-token-restore").exists())

    def test_unreviewed_credential_change_refuses_without_writing(self):
        current = b'{"core":"owner-edited-token"}\n'
        self.target.write_bytes(current)
        with self.assertRaisesRegex(execute.SuccessorExecuteError, "endpoints"):
            self.worker.restore_rollback_protected_state(self.initial)
        self.assertEqual(self.target.read_bytes(), current)
        self.assertEqual(self.initial_path.read_bytes(), self.initial_bytes)

    def test_unrelated_owner_or_mode_change_refuses(self):
        with self.subTest("mode"):
            self.target.chmod(0o400)
            with self.assertRaisesRegex(execute.SuccessorExecuteError, "permissions"):
                self.worker.restore_rollback_protected_state(self.initial)
        self.target.chmod(0o600)
        (self.state / "owner.json").write_bytes(b'{"owner":"changed"}')
        with self.assertRaisesRegex(execute.SuccessorExecuteError, "outside writer"):
            self.worker.restore_rollback_protected_state(self.initial)
        self.assertEqual(self.target.read_bytes(), self.after)

    def test_changed_backup_refuses(self):
        (self.saved / "owner.json").write_bytes(b'{}')
        with self.assertRaisesRegex(execute.SuccessorExecuteError, "baseline changed"):
            self.worker.restore_rollback_protected_state(self.initial)

    def test_symlink_target_refuses(self):
        self.target.unlink()
        self.target.symlink_to(self.saved / "writer-tokens.json")
        with self.assertRaisesRegex(execute.SuccessorExecuteError, "permissions"):
            self.worker.restore_rollback_protected_state(self.initial)
        self.assertTrue(self.target.is_symlink())

    def test_concurrent_creation_wins_exclusive_restore(self):
        real_link = os.link
        concurrent = b'{"owner":"new-writer-authority"}'

        def race(source, destination, **kwargs):
            self.target.write_bytes(concurrent)
            return real_link(source, destination, **kwargs)

        with patch.object(execute.os, "link", side_effect=race):
            with self.assertRaises(FileExistsError):
                self.worker.restore_rollback_protected_state(self.initial)
        self.assertEqual(self.target.read_bytes(), concurrent)
        self.assertEqual((self.rollback / "writer-token-restore/displaced-current").read_bytes(),
                         self.after)
        self.assertEqual(self.initial_path.read_bytes(), self.initial_bytes)

    def test_concurrent_edit_before_displacement_is_preserved(self):
        real_rename = os.rename
        concurrent = b'{"owner":"edited-before-rename"}'

        def race(source, destination):
            self.target.write_bytes(concurrent)
            return real_rename(source, destination)

        with patch.object(execute.os, "rename", side_effect=race):
            with self.assertRaisesRegex(execute.SuccessorExecuteError, "displacement"):
                self.worker.restore_rollback_protected_state(self.initial)
        self.assertEqual(self.target.read_bytes(), concurrent)
        self.assertEqual((self.rollback / "writer-token-restore/displaced-current").read_bytes(),
                         concurrent)

    def test_schema_five_routes_through_parent_without_legacy_target_mutation(self):
        self.worker.mutations_started = True
        with patch.object(execute.r11.Orchestrator, "rollback",
                          return_value={"status": "rolled_back_healthy"}) as parent:
            result = self.worker.rollback()
        parent.assert_called_once_with()
        self.assertEqual(result["preserved_concurrent_config_targets"], [])
        self.assertEqual(self.target.read_bytes(), self.after)

    def test_legacy_rollback_keeps_exact_original_guard(self):
        self.worker.artifacts = {}
        with self.assertRaisesRegex(execute.r11.ExecuteError, "protected owner state changed"):
            self.worker.verify_rollback_protected_state(self.initial)

    def test_result_binds_both_sources_and_exact_byte_hashes(self):
        result = execute.writer_transition_result(self.before, self.after, self.row)
        self.assertEqual(result["predecessor_commit"], "a" * 40)
        self.assertEqual(result["successor_commit"], "b" * 40)
        self.assertEqual(result["added_operations"], ["new"])
        self.assertNotEqual(result["before_sha256"], result["after_sha256"])
