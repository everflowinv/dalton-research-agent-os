"""Read-only workspace identity for a Cockpit bound to one runtime namespace."""

from __future__ import annotations

import os
import json
from pathlib import Path
from typing import Any, Mapping

from .workspace import WorkspaceError, load_workspace_manifest


def _legacy_host_roots(config: Any, environment: Mapping[str, str]) -> list[Path]:
    """Where the legacy environment's host root may be, most authoritative first.

    2026-09-30 (P1-4): this used to be ``DALTON_STATE_DIR/..`` only.  The live
    legacy control plist sets no ``DALTON_STATE_DIR``, and the legacy state
    directory does not sit inside the host root anyway (it is under
    ``~/Library/Application Support``), so the header fell back to "研究系统"
    while the workspace list -- which reads ``manager.json`` -- showed the
    owner's name for it.  The manager configuration names the host root; the
    configured state directory and the environment variable are fallbacks.
    """

    roots: list[Path] = []
    manager = getattr(config, "workspace_manager_config_path", None)
    if manager:
        try:
            raw = json.loads(Path(manager).expanduser().read_text())
        except (OSError, ValueError):
            raw = None
        host_root = raw.get("host_root") if isinstance(raw, Mapping) else None
        if isinstance(host_root, str) and host_root:
            roots.append(Path(host_root).expanduser())
        roots.append(Path(manager).expanduser().parent)
    state_dir = getattr(config, "state_dir", None)
    if state_dir:
        roots.append(Path(state_dir).expanduser().parent)
    if environment.get("DALTON_STATE_DIR"):
        roots.append(Path(environment["DALTON_STATE_DIR"]).expanduser().parent)
    unique: list[Path] = []
    for root in roots:
        if root not in unique:
            unique.append(root)
    return unique


def legacy_display_name(config: Any, *, environ: Mapping[str, str] | None = None) -> str | None:
    """The owner's name for the legacy environment, or ``None`` if never set."""

    environment = os.environ if environ is None else environ
    for root in _legacy_host_roots(config, environment):
        try:
            name = json.loads((root / "legacy-display" / "display.json").read_text()).get(
                "display_name")
        except (OSError, ValueError, AttributeError):
            continue
        if isinstance(name, str) and name.strip():
            return name
    return None


def cockpit_workspace_context(
    config: Any, *, writer_socket: Path, token_config: Path,
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    environment = os.environ if environ is None else environ
    manifest = environment.get("DALTON_WORKSPACE_MANIFEST")
    if manifest is None:
        return {"mode": "legacy", "slug": None, "workspace_id": None,
                "name": legacy_display_name(config, environ=environment),
                "aggregate_capacity": "unknown"}
    if not manifest:
        raise WorkspaceError("workspace manifest environment binding is empty")
    workspace = load_workspace_manifest(manifest)
    expected = {
        "core_db": workspace.state_dir / "core.sqlite",
        "state_dir": workspace.state_dir,
        "scheduler_db": workspace.state_dir / "scheduler.sqlite",
        "heartbeat_path": workspace.state_dir / "run" / "heartbeat.json",
    }
    for key, path in expected.items():
        if Path(getattr(config, key)).resolve() != path:
            raise WorkspaceError(f"Cockpit {key} differs from its workspace")
    for label, candidate in (("journal_path", config.journal_path),
                             ("token_config", token_config)):
        resolved = Path(candidate).resolve()
        if not resolved.is_relative_to(workspace.workspace_root):
            raise WorkspaceError(f"Cockpit {label} escapes its workspace")
    if Path(writer_socket).resolve() != workspace.writer_socket:
        raise WorkspaceError("Cockpit writer socket differs from its workspace")
    try:
        name = json.loads((workspace.workspace_root / "display.json").read_text()).get("display_name")
    except (OSError, ValueError):
        name = None
    return {
        "mode": "isolated", "slug": workspace.slug, "name": name or workspace.slug,
        "workspace_id": workspace.workspace_id,
        "manifest_hash": workspace.content_hash,
        "release_ref": workspace.release_ref,
        "aggregate_capacity": ("configured_model_authority"
                               if workspace.shared_capacity or workspace.shared_model_capacity_bindings
                               else "unknown"),
        "connector_aggregate_capacity": ("configured_scoped_authorities"
                                         if workspace.shared_connector_capacity else "unknown"),
    }
