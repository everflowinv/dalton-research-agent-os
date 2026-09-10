from __future__ import annotations

import plistlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from dalton_core.macos_launchagent import render
from dalton_core.workspace import create_workspace_manifest
from dalton_core.workspace_process import install_workspace, workspace_plan


class WorkspaceLaunchAgentTests(unittest.TestCase):
    def _workspace(self, root, slug, port):
        release = root / "releases" / ("a" * 64)
        (release / "bin").mkdir(parents=True, exist_ok=True)
        for executable in ("dalton-writer", "daltond"):
            (release / "bin" / executable).write_text("stub")
        return create_workspace_manifest(
            root, slug, port, "release:sha256:" + "a" * 64, release
        )

    def test_two_workspaces_get_distinct_labels_paths_and_state_arguments(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            labels = []
            for slug, port in (("analyst-a", 7411), ("analyst-b", 7412)):
                workspace = root / slug
                paths = render(
                    root / "LaunchAgents",
                    root / "release" / "bin",
                    workspace / "state",
                    workspace / "config" / "service.json",
                    workspace / "logs",
                    label_namespace=f"space.lumos.dalton.workspace.{slug}",
                )
                writer = plistlib.loads(Path(paths["writer"]).read_bytes())
                controller = plistlib.loads(Path(paths["controller"]).read_bytes())
                labels.extend((writer["Label"], controller["Label"]))
                self.assertEqual(
                    controller["ProgramArguments"][-1],
                    str((workspace / "config" / "service.json").resolve()),
                )
                self.assertEqual(
                    writer["ProgramArguments"][writer["ProgramArguments"].index("--db") + 1],
                    str((workspace / "state" / "core.sqlite").resolve()),
                )
                self.assertTrue(writer["StandardOutPath"].startswith(str(workspace.resolve())))
                self.assertNotIn(str(port), " ".join(writer["ProgramArguments"]))
            self.assertEqual(len(labels), len(set(labels)))

    def test_invalid_namespace_is_rejected_before_output(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with self.assertRaisesRegex(ValueError, "namespace"):
                render(
                    root / "agents",
                    root / "release" / "bin",
                    root / "state",
                    root / "config.json",
                    root / "logs",
                    label_namespace="space.lumos.dalton.workspace.bad/slug",
                )
            self.assertFalse((root / "agents").exists())

    def test_install_uses_shared_release_without_modifying_it_and_exports_manifest(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = self._workspace(root, "analyst-a", 17411)
            release_before = {
                path.relative_to(workspace.release_path): path.read_bytes()
                for path in workspace.release_path.rglob("*")
                if path.is_file()
            }
            plan = workspace_plan(workspace.manifest_path, root / "agents")
            self.assertFalse(plan["writes_performed"])
            result = install_workspace(workspace.manifest_path, root / "agents")
            self.assertEqual(result["release_ref"], workspace.release_ref)
            for plist_path in result["plists"].values():
                plist = plistlib.loads(Path(plist_path).read_bytes())
                self.assertEqual(
                    plist["EnvironmentVariables"]["DALTON_WORKSPACE_MANIFEST"],
                    str(workspace.manifest_path),
                )
            release_after = {
                path.relative_to(workspace.release_path): path.read_bytes()
                for path in workspace.release_path.rglob("*")
                if path.is_file()
            }
            self.assertEqual(release_before, release_after)

    def test_port_collision_is_rejected_before_any_plist_or_log_write(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            listener = __import__("socket").socket()
            self.addCleanup(listener.close)
            listener.bind(("127.0.0.1", 0))
            workspace = self._workspace(root, "analyst-a", listener.getsockname()[1])
            with self.assertRaisesRegex(Exception, "already in use"):
                install_workspace(workspace.manifest_path, root / "agents")
            self.assertFalse((root / "agents").exists())
            self.assertFalse(workspace.log_dir.exists())

    def test_two_controller_processes_are_isolated_and_one_can_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            configs = []
            tokens = []
            for slug in ("analyst-a", "analyst-b"):
                workspace = root / slug
                (workspace / "run").mkdir(parents=True)
                config = workspace / "service.json"
                config.write_text(json.dumps({"heartbeat_path": str(workspace / "run" / "heartbeat.json")}))
                token = workspace / "writer-tokens.json"
                token.write_text(slug)
                configs.append(config)
                tokens.append(token)
            program = (
                "from dalton_core.controller_singleton import ControllerOwnership;"
                "import sys,time;"
                "guard=ControllerOwnership(sys.argv[1]);guard.__enter__();"
                "print('ready',flush=True);time.sleep(60)"
            )
            env = {**os.environ, "PYTHONPATH": str(Path("src").resolve())}

            def start(config):
                process = subprocess.Popen(
                    [sys.executable, "-c", program, str(config)],
                    stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, env=env,
                )
                self.assertEqual(process.stdout.readline().strip(), "ready")
                process.stdout.close()
                return process

            first, second = start(configs[0]), start(configs[1])
            self.addCleanup(lambda: first.poll() is None and first.kill())
            self.addCleanup(lambda: second.poll() is None and second.kill())
            first.terminate()
            first.wait(timeout=5)
            self.assertIsNone(second.poll())
            self.assertEqual([path.read_text() for path in tokens], ["analyst-a", "analyst-b"])
            replacement = start(configs[0])
            replacement.terminate()
            replacement.wait(timeout=5)
            self.assertIsNone(second.poll())
            second.terminate()
            second.wait(timeout=5)


if __name__ == "__main__":
    unittest.main()
