#!/usr/bin/env python3
"""Build a read-only, reviewable plan for a new Dalton workspace canary."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import socket
import subprocess
from pathlib import Path


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def command(*parts: object) -> list[str]:
    return [os.fspath(part) if isinstance(part, Path) else str(part) for part in parts]


def prepare(args: argparse.Namespace) -> dict:
    host = args.host_root.expanduser().resolve()
    legacy = args.legacy_root.expanduser().resolve()
    wheel = args.wheel.expanduser().resolve()
    agents = args.launch_agents_dir.expanduser().resolve()
    tailscale = args.tailscale_executable.expanduser().resolve()
    if not wheel.is_file() or wheel.suffix != ".whl":
        raise RuntimeError("wheel must be an existing .whl file")
    actual = sha256(wheel)
    if actual != args.wheel_sha256:
        raise RuntimeError("wheel SHA-256 differs from the reviewed value")
    if host == legacy or host.is_relative_to(legacy) or legacy.is_relative_to(host):
        raise RuntimeError("fleet root and legacy root must not overlap")
    workspace = host / "workspaces" / args.slug
    manifest = workspace / "workspace.json"
    if workspace.exists() or workspace.is_symlink():
        raise RuntimeError("workspace target already exists")
    release = host / "runtime" / "releases" / actual / "venv"
    if not tailscale.is_file() or not os.access(tailscale, os.X_OK):
        raise RuntimeError("tailscale executable is unavailable")
    with socket.socket() as probe:
        try:
            probe.bind(("127.0.0.1", args.port))
        except OSError as exc:
            raise RuntimeError("workspace cockpit port is already in use") from exc

    namespace = f"space.lumos.dalton.workspace.{args.slug}"
    labels = [f"{namespace}.{role}" for role in ("writer", "controller", "control", "thesis-impact")]
    listed = subprocess.run(["launchctl", "list"], capture_output=True, text=True, check=False)
    collisions = [label for label in labels if label in listed.stdout]
    if collisions:
        raise RuntimeError("workspace LaunchAgent label is already loaded")

    shared = [release, *[path.expanduser().resolve() for path in args.shared_readonly_path]]
    create = command(
        release / "bin" / "dalton-workspace", "create", "--host-root", host,
        "--slug", args.slug, "--cockpit-port", args.port,
        "--release-ref", f"release:sha256:{actual}", "--release-path", release,
    )
    for path in shared:
        create += command("--shared-readonly-path", path)
    for path in args.shared_model_capacity_binding:
        create += command("--shared-model-capacity-binding", path.expanduser().resolve())
    for path in args.shared_connector_capacity_binding:
        create += command("--shared-connector-capacity-binding", path.expanduser().resolve())

    env = {"DALTON_WORKSPACE_MANIFEST": str(manifest)}
    python = release / "bin" / "python"
    return {
        "schema_version": "dalton-workspace-canary-plan-0.1",
        "writes_performed": False,
        "fleet_root": str(host),
        "legacy_root": str(legacy),
        "legacy_overlap": False,
        "workspace_root": str(workspace),
        "manifest": str(manifest),
        "cockpit_port": args.port,
        "cockpit_url": f"https://{args.tailscale_host}:{args.port}/",
        "release_path": str(release),
        "release_ref": f"release:sha256:{actual}",
        "launchagent_labels": labels,
        "preflight": {
            "wheel_sha256_verified": True,
            "port_available_at_check": True,
            "launchagent_labels_available_at_check": True,
            "core_wheelhouse_complete": True,
            "optional_provider_extras_included": False,
        },
        "commands": [
            {"phase": "install_release", "argv": command(
                args.bootstrap_python, "-m", "dalton_core.workspace_release",
                "--host-root", host, "--wheel", wheel, "--wheel-sha256", actual)},
            {"phase": "create_workspace", "argv": create},
            {"phase": "bootstrap", "env": env, "argv": command(
                release / "bin" / "dalton-bootstrap", "--state-dir",
                workspace / "state" / "dalton-core", "--config",
                workspace / "config" / "service.json", "--workspace-manifest", manifest)},
            {"phase": "configure_cockpit", "env": env, "argv": command(
                python, "-m", "dalton_core.workspace_control_setup", "--manifest", manifest,
                "--owner-login", args.owner_login, "--tailscale-host", args.tailscale_host,
                "--tailscale-executable", tailscale)},
            {"phase": "plan_processes", "env": env, "argv": command(
                python, "-m", "dalton_core.workspace_process", "plan", "--manifest", manifest,
                "--launch-agents-dir", agents)},
            {"phase": "install_processes", "env": env, "argv": command(
                python, "-m", "dalton_core.workspace_process", "install", "--manifest", manifest,
                "--launch-agents-dir", agents)},
            {"phase": "start_processes_after_workspace_mission_is_signed", "env": env,
             "argv": command(python, "-m", "dalton_core.workspace_process", "start",
                             "--manifest", manifest, "--launch-agents-dir", agents,
                             "--uid", args.uid)},
            {"phase": "publish_https_after_local_health", "argv": command(
                tailscale, "serve", "--bg", f"--https={args.port}",
                f"http://127.0.0.1:{args.port}")},
        ],
        "gates": [
            "review shared model and connector capacity bindings",
            "configure workspace-local provider and connector policies",
            "create and sign a workspace-specific mission",
            "verify local cockpit identity and health before publishing HTTPS",
            "diff tailscale serve status before and after route publication",
        ],
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host-root", type=Path, required=True)
    parser.add_argument("--legacy-root", type=Path, default=Path.home() / "Library/Application Support/Dalton")
    parser.add_argument("--wheel", type=Path, required=True)
    parser.add_argument("--wheel-sha256", required=True)
    parser.add_argument("--bootstrap-python", type=Path, required=True)
    parser.add_argument("--slug", required=True)
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--owner-login", required=True)
    parser.add_argument("--tailscale-host", required=True)
    parser.add_argument("--tailscale-executable", type=Path, required=True)
    parser.add_argument("--launch-agents-dir", type=Path, required=True)
    parser.add_argument("--uid", type=int, required=True)
    parser.add_argument("--shared-readonly-path", type=Path, action="append", default=[])
    parser.add_argument("--shared-model-capacity-binding", type=Path, action="append", default=[])
    parser.add_argument("--shared-connector-capacity-binding", type=Path, action="append", default=[])
    args = parser.parse_args()
    print(json.dumps(prepare(args), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
