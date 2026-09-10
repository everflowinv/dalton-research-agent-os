from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dalton_core.bootstrap import bootstrap
from dalton_core.lane_child_launcher import LaneChildLauncher, LaneChildRejected
from dalton_core.workspace import create_workspace_manifest
from dalton_core.workspace_runtime import (
    ENVIRONMENT_KEY,
    WorkspaceRuntimeError,
    validate_cli_state,
    validate_runtime_context,
)


RELEASE_HASH = "d" * 64
RELEASE_REF = "release:sha256:" + RELEASE_HASH


class _Launcher(LaneChildLauncher):
    TICKET_PREFIX = "runtime-test"
    TICKETS_DIRNAME = "runtime-test-runs"

    def __init__(self, *, child_state: Path, **kwargs):
        self.child_state = child_state
        super().__init__(**kwargs)

    def _command(self, *, ticket_dir: Path, **kwargs):
        return ["python3", "-m", "example", "--state-dir", str(self.child_state),
                "--summary-dir", str(ticket_dir)]


class WorkspaceRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.host = Path(self.temp.name) / "Dalton"
        self.release = self.host / "runtime" / "releases" / RELEASE_HASH
        self.release.mkdir(parents=True)
        self.one = create_workspace_manifest(
            self.host, "analyst-a", 8787, RELEASE_REF, self.release)
        self.two = create_workspace_manifest(
            self.host, "analyst-b", 8788, RELEASE_REF, self.release)

    def _env(self, workspace):
        return {ENVIRONMENT_KEY: str(workspace.manifest_path)}

    def test_two_workspace_config_cross_binding_refuses_before_bootstrap_writes(self):
        with patch.dict("os.environ", self._env(self.one), clear=True):
            bootstrap(self.one.state_dir, self.one.config_path,
                      workspace_manifest=self.one.manifest_path)
        sentinel = self.two.state_dir / "core.sqlite"
        with patch.dict("os.environ", self._env(self.one), clear=True):
            with self.assertRaisesRegex(RuntimeError, "runtime state_dir"):
                bootstrap(self.two.state_dir, self.two.config_path,
                          workspace_manifest=self.two.manifest_path)
        self.assertFalse(sentinel.exists())
        with self.assertRaisesRegex(WorkspaceRuntimeError, "bindings differ"):
            validate_runtime_context(
                config_path=self.one.config_path, environment=self._env(self.two))

    def test_bound_config_needs_environment_while_legacy_remains_compatible(self):
        with patch.dict("os.environ", self._env(self.one), clear=True):
            bootstrap(self.one.state_dir, self.one.config_path,
                      workspace_manifest=self.one.manifest_path)
        with self.assertRaisesRegex(WorkspaceRuntimeError, "requires"):
            validate_runtime_context(config_path=self.one.config_path, environment={})
        legacy = Path(self.temp.name) / "legacy.json"
        legacy.write_text(json.dumps({"schema_version": "irrelevant"}))
        self.assertIsNone(validate_runtime_context(config_path=legacy, environment={}))

    def test_stale_manifest_is_rejected(self):
        raw = json.loads(self.one.manifest_path.read_text())
        raw["cockpit_port"] += 10
        self.one.manifest_path.write_text(json.dumps(raw))
        with patch.dict("os.environ", self._env(self.one), clear=True):
            with self.assertRaisesRegex(WorkspaceRuntimeError, "manifest is invalid"):
                validate_cli_state(self.one.state_dir)

    def test_lane_constructor_rejects_cross_bound_state_before_ticket_root(self):
        with patch.dict("os.environ", self._env(self.two), clear=True):
            with self.assertRaisesRegex(Exception, "state_dir differs"):
                _Launcher(state_dir=self.one.state_dir, child_state=self.one.state_dir)
        self.assertFalse((self.one.state_dir / _Launcher.TICKETS_DIRNAME).exists())

    def test_child_argv_cross_binding_refuses_before_run_directory_or_log(self):
        self.one.state_dir.mkdir(parents=True)
        with patch.dict("os.environ", self._env(self.one), clear=True):
            launcher = _Launcher(
                state_dir=self.one.state_dir, child_state=self.two.state_dir)
            with self.assertRaisesRegex(LaneChildRejected, "state_dir differs"):
                launcher.spawn(digest="a" * 24, record={})
        run_dir = self.one.state_dir / _Launcher.TICKETS_DIRNAME / ("a" * 24)
        self.assertFalse(run_dir.exists())


if __name__ == "__main__":
    unittest.main()
