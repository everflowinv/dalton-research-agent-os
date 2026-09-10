from __future__ import annotations

import hashlib
import json
import os
import socket
import subprocess
import tempfile
import time
import unittest
import urllib.request
import zipfile
from pathlib import Path

from dalton_core.bootstrap import bootstrap
from dalton_core.workspace import create_workspace_manifest
from dalton_core.workspace_control_setup import configure_workspace_control
from dalton_core.workspace_release import install_release, validate_release


PROGRAM = r'''
import json, os, pathlib, sys, time
from http.server import BaseHTTPRequestHandler, HTTPServer
manifest = json.loads(pathlib.Path(os.environ["DALTON_WORKSPACE_MANIFEST"]).read_text())
role = pathlib.Path(sys.argv[0]).name
state = pathlib.Path(manifest["state_dir"])
(state / "run").mkdir(parents=True, exist_ok=True)
(state / "run" / (role + ".pid")).write_text(str(os.getpid()))
if role == "dalton-control":
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            body = json.dumps({"workspace_id": manifest["workspace_id"], "research": "awaiting_mission"}).encode()
            self.send_response(200); self.send_header("Content-Type", "application/json"); self.end_headers(); self.wfile.write(body)
        def log_message(self, *args): pass
    HTTPServer(("127.0.0.1", manifest["cockpit_port"]), Handler).serve_forever()
else:
    while True: time.sleep(1)
'''


def fixture_wheel(path: Path) -> str:
    dist = "dalton_workspace_fixture-0.1.dist-info"
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("dalton_core/__init__.py", "")
        archive.writestr("dalton_core/workspace_fixture.py", PROGRAM)
        archive.writestr(f"{dist}/METADATA", "Metadata-Version: 2.1\nName: dalton-workspace-fixture\nVersion: 0.1\n")
        archive.writestr(f"{dist}/WHEEL", "Wheel-Version: 1.0\nGenerator: test\nRoot-Is-Purelib: true\nTag: py3-none-any\n")
        archive.writestr(f"{dist}/entry_points.txt", "[console_scripts]\ndaltond=dalton_core.workspace_fixture:main\n")
        archive.writestr(f"{dist}/RECORD", "")
    # The fixture module intentionally executes at import. Three copied scripts
    # select their role from argv[0].
    return hashlib.sha256(path.read_bytes()).hexdigest()


class WorkspaceEndToEndTests(unittest.TestCase):
    def test_two_workspaces_share_one_release_but_not_process_or_state(self):
        with tempfile.TemporaryDirectory() as directory:
            host = Path(directory)
            wheel = host / "dalton_workspace_fixture-0.1-py3-none-any.whl"
            digest = fixture_wheel(wheel)

            def installer(_wheel, venv):
                (venv / "bin").mkdir(parents=True)
                (venv / "lib" / "python3.14" / "site-packages" / "dalton_core").mkdir(parents=True)
                (venv / "lib" / "python3.14" / "site-packages" / "dalton_core" / "__init__.py").write_text("")
                (venv / "lib" / "python3.14" / "site-packages" / "dalton_core" / "workspace_fixture.py").write_text(PROGRAM)
                for name in ("python", "daltond", "dalton-writer", "dalton-control"):
                    script = venv / "bin" / name
                    if name == "python":
                        script.write_text(f"#!/bin/sh\nexec {os.path.realpath(os.sys.executable)} \"$@\"\n")
                    else:
                        script.write_text(f"#!{venv}/bin/python\nimport dalton_core.workspace_fixture\n")
                    script.chmod(0o700)

            release = install_release(host, wheel, digest, installer=installer)
            release_path = Path(release["release_path"])
            snapshots = {
                path.relative_to(release_path): (path.read_bytes(), path.stat().st_mode & 0o777)
                for path in release_path.rglob("*") if path.is_file()
            }
            ports = []
            for _ in range(2):
                probe = socket.socket(); probe.bind(("127.0.0.1", 0))
                ports.append(probe.getsockname()[1]); probe.close()
            workspaces = []
            for slug, port in zip(("analyst-a", "analyst-b"), ports):
                workspace = create_workspace_manifest(
                    host, slug, port, f"release:sha256:{digest}", release_path,
                    shared_readonly_paths=["/usr/bin/false"],
                )
                bootstrap(workspace.state_dir, workspace.config_path, workspace_manifest=workspace.manifest_path)
                configure_workspace_control(
                    workspace.manifest_path, owner_login="owner@example.com",
                    tailscale_host=f"{slug}.example.ts.net", tailscale_executable="/usr/bin/false",
                )
                workspaces.append(workspace)

            processes = {}

            def start(workspace, role):
                env = {**os.environ, "DALTON_WORKSPACE_MANIFEST": str(workspace.manifest_path), "PYTHONDONTWRITEBYTECODE": "1"}
                env["PYTHONPATH"] = str(next(release_path.glob("lib/python*/site-packages")))
                process = subprocess.Popen([str(release_path / "bin" / role)], env=env)
                processes[(workspace.slug, role)] = process
                return process

            for workspace in workspaces:
                for role in ("dalton-writer", "daltond", "dalton-control"):
                    start(workspace, role)
            def cleanup_processes():
                for process in processes.values():
                    if process.poll() is None:
                        process.kill()
                for process in processes.values():
                    if process.poll() is None:
                        process.wait(timeout=5)
            self.addCleanup(cleanup_processes)
            for workspace in workspaces:
                deadline = time.time() + 5
                while True:
                    try:
                        payload = json.loads(urllib.request.urlopen(f"http://127.0.0.1:{workspace.cockpit_port}/", timeout=.2).read())
                        break
                    except Exception:
                        if time.time() >= deadline: raise
                        time.sleep(.05)
                self.assertEqual(payload, {"workspace_id": workspace.workspace_id, "research": "awaiting_mission"})

            b_pid = processes[("analyst-b", "daltond")].pid
            b_tokens = (workspaces[1].state_dir / "writer-tokens.json").read_bytes()
            for role in ("dalton-control", "daltond", "dalton-writer"):
                process = processes[("analyst-a", role)]; process.terminate(); process.wait(5)
            self.assertEqual(processes[("analyst-b", "daltond")].pid, b_pid)
            self.assertIsNone(processes[("analyst-b", "daltond")].poll())
            self.assertEqual((workspaces[1].state_dir / "writer-tokens.json").read_bytes(), b_tokens)
            replacement = start(workspaces[0], "daltond")
            time.sleep(.1)
            self.assertIsNone(replacement.poll())
            self.assertEqual(validate_release(release_path, digest)["wheel_sha256"], digest)
            after = {
                path.relative_to(release_path): (path.read_bytes(), path.stat().st_mode & 0o777)
                for path in release_path.rglob("*") if path.is_file()
            }
            self.assertEqual(snapshots, after)


if __name__ == "__main__":
    unittest.main()
