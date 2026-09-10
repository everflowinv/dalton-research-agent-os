from __future__ import annotations

import plistlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from dalton_core.macos_launchagent import render
from dalton_core.workspace import create_workspace_manifest
from dalton_core.workspace_process import install_workspace, workspace_plan
from dalton_core.workspace_release import install_release, validate_release
from dalton_core.bootstrap import bootstrap
from dalton_core.service import ServiceConfig
from dalton_core.workspace_control_setup import configure_workspace_control


def stub_wheel(path):
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("dalton_core/__init__.py", "VERSION = 'test'\n")
    return __import__("hashlib").sha256(path.read_bytes()).hexdigest()


def stub_install(_wheel, venv):
    (venv / "bin").mkdir(parents=True)
    for executable in ("python", "dalton-writer", "daltond"):
        path = venv / "bin" / executable
        path.write_text("stub")
        path.chmod(0o700)
    package = venv / "lib" / "python3.14" / "site-packages" / "dalton_core"
    package.mkdir(parents=True)
    package.joinpath("__init__.py").write_text("VERSION = 'test'\n")


class WorkspaceLaunchAgentTests(unittest.TestCase):
    def _workspace(self, root, slug, port):
        wheel = root / "dalton-test-py3-none-any.whl"
        digest = stub_wheel(wheel)
        release = Path(install_release(root, wheel, digest, installer=stub_install)["release_path"])
        return create_workspace_manifest(
            root, slug, port, "release:sha256:" + digest, release,
            shared_readonly_paths=["/usr/bin/false"],
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

    def test_explicit_owner_setup_binds_manifest_port_and_local_paths(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = self._workspace(root, "analyst-a", 17411)
            bootstrap(
                workspace.state_dir,
                workspace.config_path,
                workspace_manifest=workspace.manifest_path,
            )
            result = configure_workspace_control(
                workspace.manifest_path,
                owner_login="owner@example.com",
                tailscale_host="analyst-a.example.ts.net",
                tailscale_executable="/usr/bin/false",
            )
            config = ServiceConfig.from_file(workspace.config_path)
            self.assertEqual(config.control.port, workspace.cockpit_port)
            self.assertEqual(config.control.writer_socket, workspace.writer_socket)
            self.assertFalse(result["tailscale_published"])
            tokens = json.loads((workspace.state_dir / "writer-tokens.json").read_text())
            self.assertIn(
                "dashboard-control",
                [principal["principal_id"] for principal in tokens["principals"]],
            )
            again = configure_workspace_control(
                workspace.manifest_path,
                owner_login="owner@example.com",
                tailscale_host="analyst-a.example.ts.net",
                tailscale_executable="/usr/bin/false",
            )
            self.assertEqual(again["port"], workspace.cockpit_port)

    def test_concurrent_workspace_creation_cannot_claim_the_same_port(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            release = root / "release"
            release.mkdir()

            def create(slug):
                try:
                    return create_workspace_manifest(
                        root, slug, 17411, "release:sha256:" + "a" * 64, release
                    )
                except Exception as exc:
                    return exc

            with ThreadPoolExecutor(max_workers=2) as executor:
                results = list(executor.map(create, ("analyst-a", "analyst-b")))
            self.assertEqual(sum(not isinstance(row, Exception) for row in results), 1)
            self.assertEqual(sum("port" in str(row) for row in results if isinstance(row, Exception)), 1)

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


class ImmutableReleaseTests(unittest.TestCase):
    def test_real_offline_venv_console_script_runs_after_atomic_move(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            wheel = root / "dalton_test-0.1-py3-none-any.whl"
            with zipfile.ZipFile(wheel, "w") as archive:
                archive.writestr("dalton_core/__init__.py", "")
                archive.writestr(
                    "dalton_core/fixture_cli.py",
                    "def main():\n    print('final-release-ok')\n",
                )
                archive.writestr(
                    "dalton_test-0.1.dist-info/METADATA",
                    "Metadata-Version: 2.1\nName: dalton-test\nVersion: 0.1\n",
                )
                archive.writestr(
                    "dalton_test-0.1.dist-info/WHEEL",
                    "Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\nTag: py3-none-any\n",
                )
                archive.writestr(
                    "dalton_test-0.1.dist-info/entry_points.txt",
                    "[console_scripts]\ndaltond=dalton_core.fixture_cli:main\ndalton-writer=dalton_core.fixture_cli:main\n",
                )
                archive.writestr("dalton_test-0.1.dist-info/RECORD", "")
            digest = __import__("hashlib").sha256(wheel.read_bytes()).hexdigest()
            result = install_release(root, wheel, digest)
            environment = {**os.environ, "PYTHONDONTWRITEBYTECODE": "1"}
            environment.pop("PYTHONPATH", None)
            completed = subprocess.run(
                [str(Path(result["release_path"]) / "bin" / "daltond")],
                capture_output=True, text=True, check=False,
                env=environment,
            )
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(completed.stdout.strip(), "final-release-ok")

    def test_install_is_atomic_and_reinstall_only_validates(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            wheel = root / "dalton-1-py3-none-any.whl"
            digest = stub_wheel(wheel)
            calls = []

            def installer(_wheel, venv):
                calls.append(str(_wheel))
                stub_install(_wheel, venv)

            first = install_release(root, wheel, digest, installer=installer)
            before = {
                path.relative_to(first["release_path"]): path.read_bytes()
                for path in Path(first["release_path"]).rglob("*") if path.is_file()
            }
            second = install_release(
                root, wheel, digest,
                installer=lambda *_: self.fail("existing release must not be reinstalled"),
            )
            after = {
                path.relative_to(second["release_path"]): path.read_bytes()
                for path in Path(second["release_path"]).rglob("*") if path.is_file()
            }
            self.assertEqual(first["status"], "installed")
            self.assertEqual(second["status"], "existing")
            self.assertEqual(before, after)
            self.assertEqual(len(calls), 1)

    def test_failure_leaves_no_release_and_corruption_refuses(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            wheel = root / "dalton-1-py3-none-any.whl"
            digest = stub_wheel(wheel)
            with self.assertRaisesRegex(RuntimeError, "installer failed"):
                install_release(
                    root, wheel, digest,
                    installer=lambda *_: (_ for _ in ()).throw(RuntimeError("installer failed")),
                )
            release = root / "runtime" / "releases" / digest / "venv"
            self.assertFalse(release.exists())

            def installer(_wheel, venv):
                stub_install(_wheel, venv)

            install_release(root, wheel, digest, installer=installer)
            (release / "bin" / "daltond").write_bytes(b"tampered")
            with self.assertRaisesRegex(Exception, "corrupt"):
                validate_release(release, digest)


if __name__ == "__main__":
    unittest.main()
