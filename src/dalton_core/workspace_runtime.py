"""Pre-write binding between a process environment and one workspace."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Mapping, Sequence

from .workspace import WorkspaceError, WorkspacePaths, load_workspace_manifest

ENVIRONMENT_KEY = "DALTON_WORKSPACE_MANIFEST"


class WorkspaceRuntimeError(RuntimeError):
    pass


def _path(value: str | Path) -> Path:
    return Path(value).expanduser().resolve()


def validate_runtime_context(
    *, config_path: str | Path | None = None,
    state_dir: str | Path | None = None,
    core_db: str | Path | None = None,
    writer_socket: str | Path | None = None,
    environment: Mapping[str, str] | None = None,
) -> WorkspacePaths | None:
    """Validate all supplied runtime paths before their process may write."""

    env = os.environ if environment is None else environment
    manifest_value = env.get(ENVIRONMENT_KEY)
    config_raw: Mapping[str, Any] | None = None
    binding: Mapping[str, Any] | None = None
    resolved_config = None if config_path is None else _path(config_path)
    if resolved_config is not None:
        try:
            loaded = json.loads(resolved_config.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise WorkspaceRuntimeError("service config is unavailable for workspace admission") from exc
        if not isinstance(loaded, Mapping):
            raise WorkspaceRuntimeError("service config is invalid for workspace admission")
        config_raw = loaded
        raw_binding = loaded.get("workspace")
        if raw_binding is not None and not isinstance(raw_binding, Mapping):
            raise WorkspaceRuntimeError("service workspace binding is invalid")
        binding = raw_binding
    if not manifest_value:
        if binding is not None:
            raise WorkspaceRuntimeError(
                "workspace-bound service requires DALTON_WORKSPACE_MANIFEST")
        return None
    try:
        workspace = load_workspace_manifest(manifest_value)
    except WorkspaceError as exc:
        raise WorkspaceRuntimeError("runtime workspace manifest is invalid") from exc
    if binding is not None:
        expected = workspace.service_binding()
        if dict(binding) != expected:
            raise WorkspaceRuntimeError("environment and service workspace bindings differ")
    elif config_raw is not None:
        raise WorkspaceRuntimeError("workspace environment cannot run an unbound service config")
    checks = {
        "config_path": (resolved_config, workspace.config_path),
        "state_dir": (None if state_dir is None else _path(state_dir), workspace.state_dir),
        "core_db": (None if core_db is None else _path(core_db),
                    workspace.state_dir / "core.sqlite"),
        "writer_socket": (None if writer_socket is None else _path(writer_socket),
                          workspace.writer_socket),
    }
    for name, (actual, expected) in checks.items():
        if actual is not None and actual != expected:
            raise WorkspaceRuntimeError(f"runtime {name} differs from workspace manifest")
    if config_raw is not None:
        for field, expected in (
            ("core_db", workspace.state_dir / "core.sqlite"),
            ("scheduler_db", workspace.state_dir / "scheduler.sqlite"),
            ("projection_db", workspace.state_dir / "dashboard-projection.sqlite"),
            ("model_router_db", workspace.state_dir / "model-router.sqlite"),
            ("heartbeat_path", workspace.state_dir / "run" / "heartbeat.json"),
            ("writer_socket", workspace.writer_socket),
        ):
            if config_raw.get(field) is None or _path(config_raw[field]) != expected:
                raise WorkspaceRuntimeError(f"service {field} differs from workspace manifest")
    return workspace


def validate_cli_state(state_dir: str | Path) -> WorkspacePaths | None:
    """Common first line for state-dir CLIs before creating summary files."""
    return validate_runtime_context(state_dir=state_dir)


def validate_child_command(
    command: Sequence[str], *, state_dir: str | Path,
) -> WorkspacePaths | None:
    """Reject a child argv that would escape its inherited workspace."""
    workspace = validate_runtime_context(state_dir=state_dir)
    if workspace is None:
        return None
    values: dict[str, str] = {}
    for index, item in enumerate(command):
        for flag in ("--state-dir", "--db", "--socket"):
            if item == flag and index + 1 < len(command):
                values[flag] = command[index + 1]
            elif item.startswith(flag + "="):
                values[flag] = item.split("=", 1)[1]
    if "--state-dir" not in values:
        raise WorkspaceRuntimeError("workspace lane child command lacks --state-dir")
    validate_runtime_context(
        state_dir=values["--state-dir"],
        core_db=values.get("--db"), writer_socket=values.get("--socket"))
    return workspace


__all__ = [
    "ENVIRONMENT_KEY", "WorkspaceRuntimeError", "validate_child_command",
    "validate_cli_state", "validate_runtime_context",
]
