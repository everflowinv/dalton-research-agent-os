"""Read-only workspace identity for a Cockpit bound to one runtime namespace."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Mapping

from .workspace import WorkspaceError, load_workspace_manifest


def cockpit_workspace_context(
    config: Any, *, writer_socket: Path, token_config: Path,
    environ: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    environment = os.environ if environ is None else environ
    manifest = environment.get("DALTON_WORKSPACE_MANIFEST")
    if manifest is None:
        return {"mode": "legacy", "slug": None, "workspace_id": None,
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
    return {
        "mode": "isolated", "slug": workspace.slug,
        "workspace_id": workspace.workspace_id,
        "manifest_hash": workspace.content_hash,
        "release_ref": workspace.release_ref,
        "aggregate_capacity": ("configured_model_authority"
                               if workspace.shared_capacity else "unknown"),
        "connector_aggregate_capacity": "unknown",
    }
