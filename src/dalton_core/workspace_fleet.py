"""List analyst Cockpits without opening research databases or proxying writes."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any, Sequence

from .service import ServiceConfig, ServiceConfigError
from .workspace import WorkspaceError, load_workspace_manifest


def fleet_overview(host_root: str | Path) -> dict[str, Any]:
    host = Path(host_root).expanduser().resolve()
    directory = host / "workspaces"
    rows: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    seen_ports: set[int] = set()
    for manifest in sorted(directory.glob("*/workspace.json")):
        try:
            workspace = load_workspace_manifest(manifest)
            if (workspace.workspace_root.parent != directory
                    or workspace.workspace_root.name != workspace.slug):
                raise WorkspaceError("workspace is outside its declared registry entry")
            if workspace.workspace_id in seen_ids or (workspace.lifecycle in {"active","paused"} and workspace.cockpit_port in seen_ports):
                raise WorkspaceError("workspace identity or Cockpit port is duplicated")
            seen_ids.add(workspace.workspace_id)
            if workspace.lifecycle in {"active","paused"}:
                seen_ports.add(workspace.cockpit_port)
            row: dict[str, Any] = {
                "slug": workspace.slug, "workspace_id": workspace.workspace_id,
                "release_ref": workspace.release_ref,
                "state": "awaiting_bootstrap", "local_cockpit_url": None,
                "runtime_health": "not_checked",
                "model_capacity": ("configured" if workspace.shared_capacity
                                   or workspace.shared_model_capacity_bindings else "unknown"),
                "connector_capacity": ("configured_scopes" if workspace.shared_connector_capacity else "unknown"),
            }
            row["lifecycle"] = workspace.lifecycle
            if workspace.lifecycle != "active":
                from .workspace_lifecycle import archive_integrity, retained_integrity
                row.update(state=workspace.lifecycle, runtime_health="intentionally_stopped",
                           integrity=archive_integrity(host, workspace.slug) if workspace.lifecycle == "archived" else retained_integrity(workspace.state_dir) if workspace.lifecycle == "paused" else {"status": "deleted"})
                rows.append(row)
                continue
            if workspace.config_path.is_file():
                config = ServiceConfig.from_file(workspace.config_path)
                if config.workspace is None or config.workspace.workspace_id != workspace.workspace_id:
                    raise WorkspaceError("service is not bound to this workspace")
                row["state"] = "awaiting_cockpit_configuration"
                if config.control is not None:
                    row["state"] = "configured"
                    row["local_cockpit_url"] = f"http://127.0.0.1:{workspace.cockpit_port}/"
            rows.append(row)
        except (WorkspaceError, ServiceConfigError, OSError, ValueError):
            # The registry is a convenience view, never permission to fall back
            # to another database or launch an ambiguous endpoint.
            rows.append({"slug": manifest.parent.name, "state": "invalid",
                         "local_cockpit_url": None,
                         "reason": "workspace manifest or service binding is invalid"})
    return {"schema_version": "dalton-fleet-overview-0.1", "workspaces": rows,
            "read_only": True, "aggregate_budget": "not_computed",
            "endpoint_note": "Local endpoints retain each Cockpit's own authentication; remote publishing is configured separately."}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", nargs="?", default="overview", choices=("overview","pause","archive","restore","delete","keep"))
    parser.add_argument("--host-root", type=Path)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--login")
    parser.add_argument("--operation-json")
    parser.add_argument("--slug")
    parser.add_argument("--reason")
    parser.add_argument("--request-id")
    parser.add_argument("--confirm-name")
    parser.add_argument("--irreversible", action="store_true")
    parser.add_argument("--review-ack", action="store_true")
    args = parser.parse_args(argv)
    if args.operation_json is not None:
        if args.config is None or not args.login:
            parser.error("--operation-json requires --config and --login")
        from .workspace_lifecycle import transition
        result = transition(args.config, args.login, json.loads(args.operation_json))
    elif args.action == "overview":
        if args.host_root is None: parser.error("overview requires --host-root")
        result = fleet_overview(args.host_root)
    else:
        if not all((args.config, args.login, args.slug, args.reason, args.request_id)):
            parser.error("lifecycle actions require --config, --login, --slug, --reason and --request-id")
        from .workspace_lifecycle import transition
        result = transition(args.config, args.login, {"slug": args.slug, "action": args.action,
            "reason": args.reason, "request_id": args.request_id, "confirm_name": args.confirm_name,
            "irreversible": args.irreversible, "review_ack": args.review_ack})
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
