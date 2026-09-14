"""Create a blank workspace with a pinned view of shared connections.

This module deliberately has no dependency on the Cockpit HTTP plane.  A host
manager can invoke its CLI in a clean subprocess, even when the manager itself
is bound to another workspace through ``DALTON_WORKSPACE_MANIFEST``.
"""

from __future__ import annotations

import argparse
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Mapping, Sequence

from .bootstrap import bootstrap
from .store import content_hash
from .workspace import (
    WorkspaceError, WorkspacePaths, create_workspace_manifest, path_is_declared,
    load_workspace_manifest, workspace_manifest,
)
from .workspace_runtime import ENVIRONMENT_KEY


SCHEMA_VERSION = "dalton-blank-workspace-request-0.1"
METADATA_SCHEMA_VERSION = "dalton-workspace-metadata-0.1"
RECEIPT_SCHEMA_VERSION = "dalton-blank-workspace-receipt-0.1"
_REQUEST_FIELDS = {
    "schema_version", "request_id", "display_name", "host_root", "slug",
    "cockpit_port", "release_ref", "release_path", "shared_readonly_paths",
    "shared_model_capacity_bindings", "shared_connector_capacity", "connection_catalog",
}
_CATALOG_FIELDS = {"schema_version", "models", "sources", "content_hash"}
_MODEL_CATALOG_FIELDS = {
    "id", "provider", "model", "family", "adapter_ref", "credential_slot_ref",
    "capabilities", "modalities", "transport",
}
_SOURCE_CATALOG_FIELDS = {
    "id", "connector_ref", "capability_id", "auth_mode", "credential_slot_refs",
    "allowed_operations", "allowed_hosts", "transport",
}
_TRANSPORT_FIELDS = {"kind", "endpoint_ref", "socket_path", "config_path"}


def _closed(value: Any, fields: set[str], label: str) -> Mapping[str, Any]:
    if not isinstance(value, Mapping) or set(value) != fields:
        raise WorkspaceError(f"{label} has an invalid closed shape")
    return value


def _text(value: Any, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise WorkspaceError(f"{label} must be a non-empty string")
    return value.strip()


def load_shared_connection_catalog(
    value: Mapping[str, Any] | None, *, workspace: Any,
) -> dict[str, Any] | None:
    """Load one pinned, read-only display catalog without granting authority."""
    if value is None:
        return None
    binding = _closed(value, {"path", "content_hash"}, "shared_connection_catalog")
    path = Path(_text(binding["path"], "shared_connection_catalog.path")).expanduser().resolve()
    if not path_is_declared(path, workspace) or path.is_relative_to(workspace.workspace_root):
        raise WorkspaceError("shared connection catalog is outside declared read-only paths")
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise WorkspaceError("shared connection catalog is unavailable or invalid") from exc
    catalog = _closed(raw, _CATALOG_FIELDS, "shared connection catalog")
    if catalog["schema_version"] != "dalton-shared-connection-catalog-0.1":
        raise WorkspaceError("shared connection catalog schema version is unsupported")
    body = {key: catalog[key] for key in catalog if key != "content_hash"}
    if catalog["content_hash"] != content_hash(body) or binding["content_hash"] != catalog["content_hash"]:
        raise WorkspaceError("shared connection catalog content hash does not match")
    for key, fields in (("models", _MODEL_CATALOG_FIELDS), ("sources", _SOURCE_CATALOG_FIELDS)):
        rows = catalog[key]
        if not isinstance(rows, list):
            raise WorkspaceError(f"shared connection catalog {key} must be an array")
        seen: set[str] = set()
        for row in rows:
            item = _closed(row, fields, f"shared connection catalog {key} entry")
            identity = _text(item["id"], f"shared connection catalog {key} id")
            if identity in seen:
                raise WorkspaceError(f"shared connection catalog {key} ids must be unique")
            seen.add(identity)
            transport = _closed(item["transport"], _TRANSPORT_FIELDS,
                                f"shared connection catalog {key} transport")
            _text(transport["kind"], "connection transport kind")
            _text(transport["endpoint_ref"], "connection transport endpoint_ref")
            for path_key in ("socket_path", "config_path"):
                path_value = transport[path_key]
                if path_value is not None:
                    transport_path = Path(_text(path_value, path_key)).expanduser().resolve()
                    if not path_is_declared(transport_path, workspace):
                        raise WorkspaceError(
                            f"connection transport {path_key} is outside declared read-only paths")
    return {"path": str(path), "content_hash": catalog["content_hash"],
            "model_count": len(catalog["models"]), "source_count": len(catalog["sources"])}


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
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


def create_blank_workspace(
    host_root: str | Path,
    slug: str,
    cockpit_port: int,
    release_ref: str,
    release_path: str | Path,
    *,
    request_id: str,
    display_name: str,
    shared_readonly_paths: Sequence[str | Path] = (),
    shared_model_capacity_bindings: Sequence[Mapping[str, str]] = (),
    shared_connector_capacity: Sequence[Mapping[str, str]] = (),
    connection_catalog: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Create fresh authorities with no inherited research authorization."""
    request = _text(request_id, "request_id")
    name = _text(display_name, "display_name")
    if os.environ.get(ENVIRONMENT_KEY):
        raise WorkspaceError(
            "blank workspace creation requires a subprocess without a workspace binding")
    manifest_path = Path(host_root).expanduser().resolve() / "workspaces" / slug / "workspace.json"
    if manifest_path.is_file():
        workspace = load_workspace_manifest(manifest_path)
        if (workspace.cockpit_port != cockpit_port or workspace.release_ref != release_ref
                or workspace.release_path != Path(release_path).expanduser().resolve()):
            raise WorkspaceError("existing workspace differs from blank creation request")
        catalog = load_shared_connection_catalog(connection_catalog, workspace=workspace)
        display_path = workspace.workspace_root / "display.json"
        if display_path.is_file():
            held = json.loads(display_path.read_text(encoding="utf-8"))
            if held.get("request_id") != request or held.get("display_name") != name:
                raise WorkspaceError("existing workspace belongs to another creation request")
    else:
        candidate_wire = workspace_manifest(
            host_root, slug, cockpit_port, release_ref, release_path,
            shared_readonly_paths=shared_readonly_paths,
            shared_model_capacity_bindings=shared_model_capacity_bindings,
            shared_connector_capacity=shared_connector_capacity,
        )
        candidate_root = Path(candidate_wire["workspace_root"])
        candidate = WorkspacePaths.from_manifest(
            candidate_wire, manifest_path=candidate_root / "workspace.json")
        catalog = load_shared_connection_catalog(connection_catalog, workspace=candidate)
        workspace = create_workspace_manifest(
            host_root, slug, cockpit_port, release_ref, release_path,
            workspace_id=candidate.workspace_id,
            shared_readonly_paths=shared_readonly_paths,
            shared_model_capacity_bindings=shared_model_capacity_bindings,
            shared_connector_capacity=shared_connector_capacity,
        )
    bootstrap(workspace.state_dir, workspace.config_path,
              workspace_manifest=workspace.manifest_path)

    metadata_body = {
        "schema_version": METADATA_SCHEMA_VERSION,
        "workspace_id": workspace.workspace_id,
        "request_id": request,
        "display_name": name,
        "shared_connection_catalog": catalog,
    }
    metadata = {**metadata_body, "content_hash": content_hash(metadata_body)}
    metadata_path = workspace.workspace_root / "display.json"
    _atomic_json(metadata_path, metadata)
    receipt_body = {
        "schema_version": RECEIPT_SCHEMA_VERSION,
        "request_id": request,
        "workspace_id": workspace.workspace_id,
        "slug": workspace.slug,
        "manifest_path": str(workspace.manifest_path),
        "metadata_path": str(metadata_path),
        "config_path": str(workspace.config_path),
        "state": "awaiting_mission",
        "model_connections": 0 if catalog is None else catalog["model_count"],
        "data_source_connections": 0 if catalog is None else catalog["source_count"],
        "connection_catalog": catalog,
        "research_state_copied": False,
        "approvals_copied": False,
        "tokens_copied": False,
    }
    return {**receipt_body, "content_hash": content_hash(receipt_body)}


def create_from_request(value: Mapping[str, Any]) -> dict[str, Any]:
    request = _closed(value, _REQUEST_FIELDS, "blank workspace request")
    if request["schema_version"] != SCHEMA_VERSION:
        raise WorkspaceError("blank workspace request schema version is unsupported")
    return create_blank_workspace(
        request["host_root"], request["slug"], request["cockpit_port"],
        request["release_ref"], request["release_path"],
        request_id=request["request_id"], display_name=request["display_name"],
        shared_readonly_paths=request["shared_readonly_paths"],
        shared_model_capacity_bindings=request["shared_model_capacity_bindings"],
        shared_connector_capacity=request["shared_connector_capacity"],
        connection_catalog=request["connection_catalog"],
    )


def connection_catalog_projection(manifest_path: str | Path) -> dict[str, Any]:
    """Project the pinned catalog without opening any workspace database."""
    from .workspace import load_workspace_manifest

    workspace = load_workspace_manifest(manifest_path)
    display_path = workspace.workspace_root / "display.json"
    try:
        display = json.loads(display_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise WorkspaceError("workspace display metadata is unavailable or invalid") from exc
    fields = {
        "schema_version", "workspace_id", "request_id", "display_name",
        "shared_connection_catalog", "content_hash",
    }
    record = _closed(display, fields, "workspace display metadata")
    body = {key: record[key] for key in record if key != "content_hash"}
    if (record["schema_version"] != METADATA_SCHEMA_VERSION
            or record["workspace_id"] != workspace.workspace_id
            or record["content_hash"] != content_hash(body)):
        raise WorkspaceError("workspace display metadata binding is invalid")
    binding = record["shared_connection_catalog"]
    if binding is None:
        return {"schema_version": "dalton-connection-catalog-projection-0.1",
                "models": [], "sources": [], "available": False}
    verified = load_shared_connection_catalog(
        {"path": binding["path"], "content_hash": binding["content_hash"]},
        workspace=workspace,
    )
    raw = json.loads(Path(verified["path"]).read_text(encoding="utf-8"))
    return {
        "schema_version": "dalton-connection-catalog-projection-0.1",
        "models": raw["models"], "sources": raw["sources"], "available": True,
        "catalog_hash": raw["content_hash"],
    }


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--request", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        value = json.loads(args.request.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        parser.error(f"blank workspace request is unavailable or invalid: {exc}")
    print(json.dumps(create_from_request(value), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
