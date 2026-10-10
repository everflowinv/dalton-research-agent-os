"""h2: every workspace gets its own publication worker, bound to its own state.

The legacy environment's ``com.dalton.research-publication-worker`` only ever
pointed at the legacy state, so a workspace's deliverables were never
localized, checked or prepared for publication.  A worker per workspace keeps
each one on its own pinned release, its own Core and its own day ledger.
"""

from __future__ import annotations

import json
import plistlib
import tempfile
import unittest
from pathlib import Path

from dalton_core.research_output_preparation import validate_worker_config
from dalton_core.research_publication_authority import published_runtime_gate
from dalton_core.workspace_process import (
    PUBLICATION_WORKER_CONFIG,
    _run_launchctl,
    install_workspace,
    workspace_plan,
)
from dalton_core.workspace import WorkspaceError
from tests.test_workspace_process import WorkspaceLaunchAgentTests


class WorkspacePublicationWorkerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.workspace = WorkspaceLaunchAgentTests._workspace(
            None, self.root, "analyst-w", 17431)
        self.agents = self.root / "agents"

    def test_install_seeds_a_config_whose_every_path_is_inside_the_workspace_state(self):
        result = install_workspace(self.workspace.manifest_path, self.agents)
        self.assertEqual(result["publication_worker_config"], "created")
        config_path = self.workspace.state_dir / PUBLICATION_WORKER_CONFIG
        config = json.loads(config_path.read_text())
        validate_worker_config(config)
        state = self.workspace.state_dir
        for key in ("core_db", "scheduler_db", "model_config", "verifier_config",
                    "checker_config", "brain_config", "work_dir", "output_directory"):
            self.assertTrue(Path(config[key]).is_relative_to(state), key)
        self.assertEqual(config["publication_gate"],
                         {"workspace_manifest": str(self.workspace.manifest_path)})
        self.assertLessEqual(config["workers"], 4)

    def test_an_existing_config_is_preserved(self):
        config_path = self.workspace.state_dir / PUBLICATION_WORKER_CONFIG
        config_path.parent.mkdir(parents=True, exist_ok=True)
        config_path.write_text(json.dumps({"owner": "edited"}))
        with self.assertRaises(ValueError):
            # The owner's file is kept as is -- and still validated, loudly.
            install_workspace(self.workspace.manifest_path, self.agents)
        self.assertEqual(json.loads(config_path.read_text()), {"owner": "edited"})

    def test_the_worker_plist_runs_this_workspaces_release_on_its_own_config(self):
        result = install_workspace(self.workspace.manifest_path, self.agents)
        label = "space.lumos.dalton.workspace.analyst-w.publication-worker"
        path = Path(result["plists"]["publication_worker"])
        self.assertEqual(path.name, f"{label}.plist")
        plist = plistlib.loads(path.read_bytes())
        self.assertEqual(plist["Label"], label)
        self.assertEqual(plist["ProgramArguments"], [
            str(self.workspace.release_path / "bin" / "python"), "-m",
            "dalton_core.research_output_preparation", "run-worker", "--config",
            str(self.workspace.state_dir / PUBLICATION_WORKER_CONFIG)])
        self.assertEqual(plist["StartInterval"], 300)
        self.assertTrue(plist["RunAtLoad"])
        self.assertEqual(plist["ProcessType"], "Background")
        self.assertNotIn("KeepAlive", plist)
        self.assertEqual(plist["EnvironmentVariables"]["DALTON_WORKSPACE_MANIFEST"],
                         str(self.workspace.manifest_path))
        self.assertTrue(plist["StandardOutPath"].endswith("publication-worker.stdout.log"))
        plan = workspace_plan(self.workspace.manifest_path, self.agents)
        self.assertIn(label, plan["labels"])

    def test_start_and_stop_include_the_worker_and_tolerate_its_absence(self):
        calls = []

        class Done:
            returncode = 0
            stderr = ""

        def run(command, **_kwargs):
            calls.append(command)
            return Done()

        # No plists rendered at all except the required two: the worker, like
        # control, is optional at start.
        self.agents.mkdir()
        for role in ("writer", "controller"):
            (self.agents / f"space.lumos.dalton.workspace.analyst-w.{role}.plist").write_text("x")
        started = _run_launchctl(self.workspace.manifest_path, self.agents, "start",
                                 uid=501, run=run)
        self.assertNotIn("space.lumos.dalton.workspace.analyst-w.publication-worker",
                         started["labels"])
        install_workspace(self.workspace.manifest_path, self.agents)
        started = _run_launchctl(self.workspace.manifest_path, self.agents, "start",
                                 uid=501, run=run)
        self.assertIn("space.lumos.dalton.workspace.analyst-w.publication-worker",
                      started["labels"])
        stopped = _run_launchctl(self.workspace.manifest_path, self.agents, "stop",
                                 uid=501, run=run)
        self.assertEqual(stopped["labels"][-1], "space.lumos.dalton.workspace.analyst-w.writer")
        self.assertIn("space.lumos.dalton.workspace.analyst-w.publication-worker",
                      stopped["labels"])


class WorkspaceStopToleratesUnloadedRolesTests(unittest.TestCase):
    def test_a_role_that_was_never_loaded_counts_as_stopped(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        workspace = WorkspaceLaunchAgentTests._workspace(None, root, "analyst-s", 17433)
        agents = root / "agents"
        agents.mkdir()

        class Done:
            def __init__(self, code: int = 0, err: str = "") -> None:
                self.returncode, self.stderr, self.stdout = code, err, ""

        def run(command, **_kwargs):
            if command[:2] == ["launchctl", "bootout"] and command[-1].endswith(".thesis-impact"):
                return Done(3, "Boot-out failed: 3: No such process")
            if command[:2] == ["launchctl", "bootout"] and command[-1].endswith(".control"):
                return Done(113, "Could not find service")
            return Done()

        stopped = _run_launchctl(workspace.manifest_path, agents, "stop", uid=501, run=run)
        self.assertEqual(stopped["status"], "stopped")
        self.assertEqual(stopped["labels"][-1], "space.lumos.dalton.workspace.analyst-s.writer")

    def test_a_real_bootout_failure_still_stops_the_run(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        root = Path(temp.name)
        workspace = WorkspaceLaunchAgentTests._workspace(None, root, "analyst-t", 17434)

        class Done:
            def __init__(self, code: int = 0, err: str = "") -> None:
                self.returncode, self.stderr, self.stdout = code, err, ""

        def run(command, **_kwargs):
            if command[:2] == ["launchctl", "bootout"]:
                return Done(5, "Boot-out failed: 5: Input/output error")
            return Done()

        with self.assertRaises(WorkspaceError):
            _run_launchctl(workspace.manifest_path, root, "stop", uid=501, run=run)


class WorkspaceGateTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.workspace = WorkspaceLaunchAgentTests._workspace(
            None, self.root, "analyst-g", 17432)
        self.gate = {"workspace_manifest": str(self.workspace.manifest_path)}

    def test_active_only_under_the_workspaces_pinned_release(self):
        active = published_runtime_gate(self.gate, prefix=str(self.workspace.release_path))
        self.assertEqual(active["status"], "active")
        self.assertEqual(active["release_ref"], self.workspace.release_ref)
        other = published_runtime_gate(self.gate, prefix=str(self.root / "other-venv"))
        self.assertEqual(other["status"], "waiting_for_release_publication")
        self.assertEqual(other["model_calls"], 0)

    def test_an_unreadable_manifest_waits_and_a_relative_one_is_invalid(self):
        missing = published_runtime_gate({"workspace_manifest": str(self.root / "nope.json")})
        self.assertEqual(missing["status"], "waiting_for_release_publication")
        relative = published_runtime_gate({"workspace_manifest": "workspace.json"})
        self.assertEqual(relative["status"], "invalid_release_authority")

    def test_the_legacy_gate_shape_is_unchanged(self):
        legacy = published_runtime_gate({
            "release_pointer": str(self.root.resolve() / "r.json"),
            "runtime_pointer": str(self.root.resolve() / "c.json"),
            "expected_release_ref": "foundation-r25", "expected_source_commit": "a" * 40})
        self.assertEqual(legacy["status"], "waiting_for_release_publication")
        mixed = published_runtime_gate({**self.gate, "expected_release_ref": "foundation-r25"})
        self.assertEqual(mixed["status"], "invalid_release_authority")


if __name__ == "__main__":
    unittest.main()
