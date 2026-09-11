"""Explicitly configure a workspace-local Cockpit control service."""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Sequence

from .cockpit_setup import install as install_cockpit
from .bootstrap import bootstrap
from .service import ServiceConfig
from .workspace import WorkspaceError, load_workspace_manifest
from .workspace import validate_service_mapping_paths


def _atomic(path: Path, value: dict[str, Any]) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def configure_workspace_control(
    manifest_path: str | Path,
    *,
    owner_login: str,
    tailscale_host: str,
    tailscale_executable: str | Path,
) -> dict[str, Any]:
    workspace = load_workspace_manifest(manifest_path)
    login = owner_login.strip() if isinstance(owner_login, str) else ""
    if not login:
        raise WorkspaceError("owner_login is required")
    config = workspace.config_path
    try:
        raw = json.loads(config.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise WorkspaceError("bootstrap the workspace before configuring Cockpit") from exc
    desired = {
        "enabled": True,
        "config": {
            "host": "127.0.0.1",
            "port": workspace.cockpit_port,
            "tailscale_host": tailscale_host,
            "tailscale_executable": str(Path(tailscale_executable).expanduser().resolve()),
            "allowed_tailscale_logins": [login],
            "writer_socket": str(workspace.writer_socket),
            "token_config": str(workspace.state_dir / "writer-tokens.json"),
            "endpoint_ref": f"workspace:{workspace.workspace_id}:cockpit",
            "feedback_timeout_seconds": 86400,
            "sweep_interval_seconds": 60,
        },
    }
    existing = raw.get("control")
    if existing is not None:
        existing_base = json.loads(json.dumps(existing))
        if isinstance(existing_base.get("config"), dict):
            existing_base["config"].pop("cockpit", None)
            # Human Intent is an installed extension of the control plane.
            # Re-running the base workspace setup must preserve it rather
            # than treating its closed configuration as a conflicting base.
            existing_base["config"].pop("intent_composer", None)
        if existing_base != desired:
            raise WorkspaceError("workspace control is already configured differently")
    original = config.read_bytes()
    if existing is None:
        candidate = {**raw, "control": desired}
        ServiceConfig.from_mapping(candidate)
        validate_service_mapping_paths(candidate, workspace)
        _atomic(config, candidate)
    try:
        bootstrap(
            workspace.state_dir,
            workspace.config_path,
            workspace_manifest=workspace.manifest_path,
        )
        cockpit = install_cockpit(config)
        ServiceConfig.from_file(config)
    except BaseException:
        descriptor, temporary = tempfile.mkstemp(prefix=f".{config.name}.", dir=config.parent)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(original)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, config)
        raise
    return {
        "status": "configured",
        "workspace_id": workspace.workspace_id,
        "port": workspace.cockpit_port,
        "owner_login": login,
        "cockpit": cockpit["cockpit"],
        "tailscale_published": False,
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--owner-login", required=True)
    parser.add_argument("--tailscale-host", required=True)
    parser.add_argument("--tailscale-executable", type=Path, required=True)
    args = parser.parse_args(argv)
    print(json.dumps(configure_workspace_control(
        args.manifest, owner_login=args.owner_login,
        tailscale_host=args.tailscale_host,
        tailscale_executable=args.tailscale_executable,
    ), sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
