"""Plan and manage one namespaced Dalton workspace process set."""

from __future__ import annotations

import argparse
import json
import os
import plistlib
import socket
import subprocess
from pathlib import Path
from typing import Any, Callable, Sequence

from .macos_launchagent import render
from .workspace import WorkspaceError, load_workspace_manifest
from .workspace_release import validate_release


def label_namespace(slug: str) -> str:
    return f"space.lumos.dalton.workspace.{slug}"


def workspace_plan(
    manifest_path: str | Path,
    launch_agents_dir: str | Path,
    *,
    check_port: bool = True,
) -> dict[str, Any]:
    workspace = load_workspace_manifest(manifest_path)
    validate_release(
        workspace.release_path,
        workspace.release_ref.removeprefix("release:sha256:"),
    )
    release_bin = workspace.release_path / "bin"
    required = ("python", "dalton-writer", "daltond")
    missing = [
        name
        for name in required
        if not (release_bin / name).is_file() or not os.access(release_bin / name, os.X_OK)
    ]
    if missing:
        raise WorkspaceError("release is missing executables: " + ", ".join(missing))
    if check_port:
        probe = socket.socket()
        try:
            probe.bind(("127.0.0.1", workspace.cockpit_port))
        except OSError as exc:
            raise WorkspaceError("cockpit_port is already in use") from exc
        finally:
            probe.close()
    namespace = label_namespace(workspace.slug)
    return {
        "status": "planned",
        "writes_performed": False,
        "workspace_id": workspace.workspace_id,
        "workspace_manifest": str(workspace.manifest_path),
        "workspace_manifest_hash": workspace.content_hash,
        "release_ref": workspace.release_ref,
        "release_path": str(workspace.release_path),
        "cockpit_port": workspace.cockpit_port,
        "labels": [f"{namespace}.{role}" for role in ("writer", "controller", "control", "thesis-impact")],
        "launch_agents_dir": str(Path(launch_agents_dir).expanduser().resolve()),
    }


def install_workspace(manifest_path: str | Path, launch_agents_dir: str | Path) -> dict[str, Any]:
    plan = workspace_plan(manifest_path, launch_agents_dir)
    workspace = load_workspace_manifest(manifest_path)
    paths = render(
        launch_agents_dir,
        workspace.release_path / "bin",
        workspace.state_dir,
        workspace.config_path,
        workspace.log_dir,
        label_namespace=label_namespace(workspace.slug),
        workspace_manifest_path=workspace.manifest_path,
    )
    return {**plan, "status": "installed", "writes_performed": True, "plists": paths}


def _run_launchctl(
    manifest_path: str | Path,
    launch_agents_dir: str | Path,
    action: str,
    *,
    uid: int,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> dict[str, Any]:
    workspace = load_workspace_manifest(manifest_path)
    namespace = label_namespace(workspace.slug)
    directory = Path(launch_agents_dir).expanduser().resolve()
    results = []
    roles = (
        ("writer", "controller", "control", "thesis-impact")
        if action == "start"
        else ("controller", "control", "thesis-impact", "writer")
    )
    for role in roles:
        if action == "stop" and role == "writer":
            drain = run(
                [
                    str(workspace.release_path / "bin" / "python"),
                    "-m", "dalton_core.launch_drain", "--state-dir",
                    str(workspace.state_dir), "--timeout", "600",
                ],
                capture_output=True, text=True, check=False,
            )
            if drain.returncode != 0:
                raise WorkspaceError(
                    "workspace lane drain failed; writer remains loaded: "
                    + drain.stderr.strip()[:300]
                )
        label = f"{namespace}.{role}"
        plist = directory / f"{label}.plist"
        if action == "start" and not plist.is_file():
            if role in {"control", "thesis-impact"}:
                continue
            raise WorkspaceError(f"required LaunchAgent is missing: {plist.name}")
        command = (
            ["launchctl", "bootstrap", f"gui/{uid}", str(plist)]
            if action == "start"
            else ["launchctl", "bootout", f"gui/{uid}/{label}"]
        )
        completed = run(command, capture_output=True, text=True, check=False)
        if completed.returncode != 0 and not (action == "stop" and "Could not find service" in completed.stderr):
            raise WorkspaceError(f"launchctl {action} failed for {label}: {completed.stderr.strip()[:300]}")
        results.append(label)
    return {"status": "started" if action == "start" else "stopped", "workspace_id": workspace.workspace_id, "labels": results}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("plan", "install", "start", "stop"))
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--launch-agents-dir", type=Path, required=True)
    parser.add_argument("--uid", type=int)
    args = parser.parse_args(argv)
    if args.action == "plan":
        result = workspace_plan(args.manifest, args.launch_agents_dir)
    elif args.action == "install":
        result = install_workspace(args.manifest, args.launch_agents_dir)
    else:
        if args.uid is None:
            parser.error("--uid is required for start/stop")
        result = _run_launchctl(args.manifest, args.launch_agents_dir, args.action, uid=args.uid)
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
