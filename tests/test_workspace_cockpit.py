from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from dalton_core.cockpit_plane import CockpitPlane
from dalton_core.workspace import WorkspaceError, create_workspace_manifest
from dalton_core.workspace_cockpit import cockpit_workspace_context


class WorkspaceCockpitTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.host = Path(folder.name)
        release = self.host / "release"
        release.mkdir()
        self.one = create_workspace_manifest(
            self.host, "analyst-a", 18910, "release:sha256:" + "a" * 64, release)
        self.two = create_workspace_manifest(
            self.host, "analyst-b", 18911, "release:sha256:" + "a" * 64, release)

    def config(self, workspace):
        return SimpleNamespace(
            core_db=workspace.state_dir / "core.sqlite", state_dir=workspace.state_dir,
            scheduler_db=workspace.state_dir / "scheduler.sqlite",
            heartbeat_path=workspace.state_dir / "run" / "heartbeat.json",
            journal_path=workspace.state_dir / "cockpit.sqlite")

    def context(self, workspace, **changes):
        config = self.config(workspace)
        config.__dict__.update(changes)
        return cockpit_workspace_context(
            config, writer_socket=workspace.writer_socket,
            token_config=workspace.state_dir / "writer-tokens.json",
            environ={"DALTON_WORKSPACE_MANIFEST": str(workspace.manifest_path)})

    def test_each_cockpit_has_distinct_identity_and_no_fake_aggregate_budget(self):
        a, b = self.context(self.one), self.context(self.two)
        self.assertNotEqual(a["workspace_id"], b["workspace_id"])
        self.assertEqual(a["slug"], "analyst-a")
        self.assertEqual(a["release_ref"], b["release_ref"])
        self.assertEqual(a["aggregate_capacity"], "unknown")
        self.assertNotIn(str(self.host), str(a))

    def test_other_workspace_core_journal_socket_and_tokens_are_rejected(self):
        for key, value in vars(self.config(self.two)).items():
            with self.subTest(key=key), self.assertRaises(WorkspaceError):
                self.context(self.one, **{key: value})
        for key, value in (("writer_socket", self.two.writer_socket),
                           ("token_config", self.two.state_dir / "writer-tokens.json")):
            kwargs = dict(writer_socket=self.one.writer_socket,
                          token_config=self.one.state_dir / "writer-tokens.json")
            kwargs[key] = value
            with self.subTest(key=key), self.assertRaises(WorkspaceError):
                cockpit_workspace_context(
                    self.config(self.one), **kwargs,
                    environ={"DALTON_WORKSPACE_MANIFEST": str(self.one.manifest_path)})

    def test_mismatched_environment_fails_before_cockpit_journal_creation(self):
        config = self.config(self.two)
        with patch.dict("os.environ", {"DALTON_WORKSPACE_MANIFEST": str(self.one.manifest_path)}):
            with self.assertRaises(WorkspaceError):
                CockpitPlane(config, writer_socket=self.two.writer_socket,
                             token_config=self.two.state_dir / "writer-tokens.json")
        self.assertFalse(config.journal_path.exists())
        self.assertFalse(self.two.state_dir.exists())

    def test_legacy_config_does_not_discover_neighboring_workspaces(self):
        result = cockpit_workspace_context(
            self.config(self.one), writer_socket=self.one.writer_socket,
            token_config=self.one.state_dir / "writer-tokens.json", environ={})
        self.assertEqual(result["mode"], "legacy")
        self.assertIsNone(result["workspace_id"])

