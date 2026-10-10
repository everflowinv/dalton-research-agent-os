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

from .macos_launchagent import _atomic_plist, _launchd_log_dir, render
from .workspace import WorkspaceError, load_workspace_manifest
from .workspace_release import validate_release


def label_namespace(slug: str) -> str:
    return f"space.lumos.dalton.workspace.{slug}"


PUBLICATION_WORKER_ROLE = "publication-worker"
PUBLICATION_WORKER_CONFIG = "research-publication-worker-config.json"
#: Every LaunchAgent role a workspace may have, in start order.
ROLES = ("writer", "controller", "control", "thesis-impact", PUBLICATION_WORKER_ROLE)
#: Roles whose plist may legitimately be absent.
OPTIONAL_ROLES = frozenset({"control", "thesis-impact", PUBLICATION_WORKER_ROLE})


def publication_worker_config(workspace: Any) -> dict[str, Any]:
    """The worker configuration for one workspace, every path inside its state.

    One worker per workspace rather than one worker enumerating every
    environment on the host (h2, 2026-09-30).  The per-workspace shape is the
    safer one:

    * it runs under the workspace's *own pinned release* -- the gate below is
      ``{"workspace_manifest": ...}``, active only when the interpreter is the
      manifest's release -- so a release switch in one environment never
      drives another environment's preparation;
    * it reads only its own state (Core read-only, its own work_dir and lock),
      so one environment's broken database or crash-looping product cannot
      stall localization in another;
    * every paid call goes through the workspace's own model configurations,
      so it is admitted against the workspace's own day ledger
      (``thesis-impact-budget.sqlite`` via ``ledger_gate(budget_db)``) and the
      host's shared daily budget binding.  Per-workspace spend caps therefore
      hold by construction; a host-wide worker would have had to re-derive
      which ledger each call belongs to.
    """

    state = Path(workspace.state_dir)
    return {
        "schema_version": "research-publication-worker-config:0.1",
        "core_db": str(state / "core.sqlite"),
        "scheduler_db": str(state / "scheduler.sqlite"),
        "model_config": str(state / "research-localization-draft-model-config.json"),
        "verifier_config": str(state / "research-localization-verifier-model-config.json"),
        "checker_config": str(state / "research-language-check-model-config.json"),
        "brain_config": str(state / "research-language-revision-model-config.json"),
        "work_dir": str(state / "research-publication-work"),
        "output_directory": str(state / "research-localization"),
        # Bounded below legacy's 4: a workspace shares the host broker.
        "workers": 2,
        "chunk_chars": 4500,
        "draft_attempts": 2,
        "max_cost_per_call": 1.0,
        "publication_gate": {"workspace_manifest": str(workspace.manifest_path)},
    }


def _seed_worker_config(path: Path, value: dict[str, Any]) -> str:
    """Write the worker config once; an owner-edited file is never replaced."""

    if path.exists() or path.is_symlink():
        return "preserved"
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        try:
            os.link(temporary, path)
        except FileExistsError:
            return "preserved"
        return "created"
    finally:
        temporary.unlink(missing_ok=True)


def render_publication_worker(workspace: Any, launch_agents_dir: str | Path) -> dict[str, str]:
    """Seed the worker config and render this workspace's worker LaunchAgent."""

    from .research_output_preparation import validate_worker_config

    config_path = Path(workspace.state_dir) / PUBLICATION_WORKER_CONFIG
    seeded = _seed_worker_config(config_path, publication_worker_config(workspace))
    validate_worker_config(json.loads(config_path.read_text(encoding="utf-8")))
    namespace = label_namespace(workspace.slug)
    label = f"{namespace}.{PUBLICATION_WORKER_ROLE}"
    logs = _launchd_log_dir(Path(workspace.log_dir).expanduser().resolve(), namespace)
    logs.mkdir(mode=0o700, parents=True, exist_ok=True)
    destination = Path(launch_agents_dir).expanduser().resolve()
    plist_path = destination / f"{label}.plist"
    _atomic_plist(plist_path, {
        "Label": label,
        "ProgramArguments": [
            str(workspace.release_path / "bin" / "python"), "-m",
            "dalton_core.research_output_preparation", "run-worker",
            "--config", str(config_path),
        ],
        "EnvironmentVariables": {
            "PYTHONDONTWRITEBYTECODE": "1", "PYTHONUNBUFFERED": "1",
            "DALTON_WORKSPACE_MANIFEST": str(workspace.manifest_path),
        },
        "RunAtLoad": True,
        "StartInterval": 300,
        "ProcessType": "Background",
        "Umask": 0o077,
        "WorkingDirectory": str(workspace.state_dir),
        "StandardOutPath": str(logs / "publication-worker.stdout.log"),
        "StandardErrorPath": str(logs / "publication-worker.stderr.log"),
    })
    return {"plist": str(plist_path), "config": str(config_path), "config_status": seeded}


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
            # Match HTTPServer's reuse setting: recently closed connections
            # must not make a stopped workspace look like a live listener.
            probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
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
        "labels": [f"{namespace}.{role}" for role in ROLES],
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
    worker = render_publication_worker(workspace, launch_agents_dir)
    paths = {**paths, "publication_worker": worker["plist"]}
    return {**plan, "status": "installed", "writes_performed": True, "plists": paths,
            "publication_worker_config": worker["config_status"]}


def _not_loaded(stderr: str) -> bool:
    return "Could not find service" in stderr or "No such process" in stderr


def _run_launchctl(
    manifest_path: str | Path,
    launch_agents_dir: str | Path,
    action: str,
    *,
    uid: int,
    run: Callable[..., subprocess.CompletedProcess[str]] = subprocess.run,
) -> dict[str, Any]:
    workspace = load_workspace_manifest(manifest_path)
    if action == "start" and workspace.lifecycle != "active":
        raise WorkspaceError("paused, archived or deleted workspaces cannot start")
    namespace = label_namespace(workspace.slug)
    directory = Path(launch_agents_dir).expanduser().resolve()
    results = []
    roles = (
        ROLES
        if action == "start"
        else ("controller", "control", "thesis-impact", PUBLICATION_WORKER_ROLE, "writer")
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
            if role in OPTIONAL_ROLES:
                continue
            raise WorkspaceError(f"required LaunchAgent is missing: {plist.name}")
        command = (
            ["launchctl", "bootstrap", f"gui/{uid}", str(plist)]
            if action == "start"
            else ["launchctl", "bootout", f"gui/{uid}/{label}"]
        )
        completed = run(command, capture_output=True, text=True, check=False)
        # A service that is not loaded is already stopped. launchctl says so in
        # two ways: "Could not find service" (113) and, for a label it knows but
        # that has no running instance, "Boot-out failed: 3: No such process"
        # (live 2026-10-01: ws-e399's never-started thesis-impact role).
        if completed.returncode != 0 and not (action == "stop" and _not_loaded(completed.stderr)):
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
