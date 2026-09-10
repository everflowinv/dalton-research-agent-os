"""Closed, owner-local identities and paths for isolated Dalton workspaces."""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import re
import tempfile
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from .store import content_hash

SCHEMA_VERSION = "dalton-workspace-0.1"
_SLUG = re.compile(r"^[a-z][a-z0-9-]{0,47}$")
_SHA = re.compile(r"^[0-9a-f]{64}$")
_REF = re.compile(r"^[A-Za-z][A-Za-z0-9._-]*:[^\s]+$")
_FIELDS = {
    "schema_version", "workspace_id", "slug", "workspace_root", "state_dir",
    "config_path", "log_dir", "spool_dir", "writer_socket", "cockpit_port",
    "release_ref", "release_path", "shared_readonly_paths", "content_hash",
}
_OPTIONAL_FIELDS = {
    "shared_capacity", "shared_connector_capacity",
    "shared_model_capacity_bindings",
}


class WorkspaceError(RuntimeError):
    pass


def _absolute(value: Any, name: str) -> Path:
    if not isinstance(value, str) or not value:
        raise WorkspaceError(f"{name} must be a non-empty absolute path")
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise WorkspaceError(f"{name} must be an absolute path")
    return path.resolve()


def _inside(path: Path, root: Path, name: str) -> Path:
    try:
        path.relative_to(root)
    except ValueError as exc:
        raise WorkspaceError(f"{name} escapes workspace_root") from exc
    if path == root:
        raise WorkspaceError(f"{name} must be below workspace_root")
    return path


@dataclass(frozen=True, slots=True)
class WorkspacePaths:
    workspace_id: str
    slug: str
    workspace_root: Path
    state_dir: Path
    config_path: Path
    log_dir: Path
    spool_dir: Path
    writer_socket: Path
    cockpit_port: int
    release_ref: str
    release_path: Path
    shared_readonly_paths: tuple[Path, ...]
    shared_capacity: Mapping[str, str] | None
    shared_model_capacity_bindings: tuple[Mapping[str, str], ...]
    shared_connector_capacity: tuple[Mapping[str, str], ...]
    content_hash: str
    manifest_path: Path | None = None

    @classmethod
    def from_manifest(
        cls, value: Mapping[str, Any], *, manifest_path: str | Path | None = None
    ) -> "WorkspacePaths":
        if (not isinstance(value, Mapping) or not _FIELDS.issubset(value)
                or set(value) - _FIELDS - _OPTIONAL_FIELDS):
            raise WorkspaceError("workspace manifest has an invalid closed shape")
        body = {key: value[key] for key in value if key != "content_hash"}
        if value["schema_version"] != SCHEMA_VERSION:
            raise WorkspaceError("workspace manifest schema version is unsupported")
        if value["content_hash"] != content_hash(body):
            raise WorkspaceError("workspace manifest content hash does not match")
        try:
            workspace_id = str(uuid.UUID(value["workspace_id"]))
        except (ValueError, TypeError, AttributeError) as exc:
            raise WorkspaceError("workspace_id must be a canonical UUID") from exc
        if workspace_id != value["workspace_id"]:
            raise WorkspaceError("workspace_id must be a canonical UUID")
        slug = value["slug"]
        if not isinstance(slug, str) or _SLUG.fullmatch(slug) is None:
            raise WorkspaceError("workspace slug is invalid")
        root = _absolute(value["workspace_root"], "workspace_root")
        state = _inside(_absolute(value["state_dir"], "state_dir"), root, "state_dir")
        config = _inside(_absolute(value["config_path"], "config_path"), root, "config_path")
        logs = _inside(_absolute(value["log_dir"], "log_dir"), root, "log_dir")
        spool = _inside(_absolute(value["spool_dir"], "spool_dir"), root, "spool_dir")
        socket = _inside(_absolute(value["writer_socket"], "writer_socket"), root, "writer_socket")
        expected = {
            "state_dir": root / "state" / "dalton-core",
            "config_path": root / "config" / "service.json",
            "log_dir": root / "logs",
            "spool_dir": root / "state" / "dalton-core" / "connector-spool",
            "writer_socket": root / "state" / "dalton-core" / "run" / "writer.sock",
        }
        actual = {"state_dir": state, "config_path": config, "log_dir": logs,
                  "spool_dir": spool, "writer_socket": socket}
        for name, expected_path in expected.items():
            if actual[name] != expected_path.resolve():
                raise WorkspaceError(f"{name} is not the canonical workspace path")
        port = value["cockpit_port"]
        if isinstance(port, bool) or not isinstance(port, int) or not 1024 <= port <= 65535:
            raise WorkspaceError("cockpit_port must be an integer from 1024 through 65535")
        release_ref = value["release_ref"]
        if not isinstance(release_ref, str) or not release_ref.startswith("release:sha256:") \
                or _SHA.fullmatch(release_ref.removeprefix("release:sha256:")) is None:
            raise WorkspaceError("release_ref must bind a lowercase SHA-256")
        release = _absolute(value["release_path"], "release_path")
        if not release.is_dir():
            raise WorkspaceError("release_path must name an installed release directory")
        shared = value["shared_readonly_paths"]
        if not isinstance(shared, list) or any(not isinstance(item, str) for item in shared):
            raise WorkspaceError("shared_readonly_paths must be an array of absolute paths")
        shared_paths = tuple(_absolute(item, "shared_readonly_paths item") for item in shared)
        if len(set(shared_paths)) != len(shared_paths):
            raise WorkspaceError("shared_readonly_paths must be unique")
        host_root = root.parent.parent
        workspaces_root = root.parent
        for path in shared_paths:
            if (path == Path(path.anchor) or path == host_root
                    or path == workspaces_root or path.is_relative_to(workspaces_root)
                    or host_root.is_relative_to(path)
                    or workspaces_root.is_relative_to(path)):
                raise WorkspaceError("shared read-only path is dangerously broad or overlaps workspaces")
        if release not in shared_paths:
            raise WorkspaceError("release_path must be declared shared read-only")
        capacity = value.get("shared_capacity")
        if capacity is not None:
            if not isinstance(capacity, Mapping) or set(capacity) != {
                    "database", "policy_ref", "policy_hash"}:
                raise WorkspaceError("shared_capacity has an invalid closed shape")
            database = _absolute(capacity["database"], "shared_capacity.database")
            if database.parent != (host_root / "fleet-capacity").resolve():
                raise WorkspaceError(
                    "shared capacity database must be directly under host fleet-capacity")
            if (not isinstance(capacity["policy_ref"], str)
                    or ":" not in capacity["policy_ref"]):
                raise WorkspaceError("shared_capacity.policy_ref is invalid")
            if (not isinstance(capacity["policy_hash"], str)
                    or _SHA.fullmatch(capacity["policy_hash"]) is None):
                raise WorkspaceError("shared_capacity.policy_hash must be lowercase SHA-256")
            capacity = dict(capacity)
        model_capacity = value.get("shared_model_capacity_bindings", [])
        if not isinstance(model_capacity, list):
            raise WorkspaceError("shared_model_capacity_bindings must be an array")
        if capacity is not None and model_capacity:
            raise WorkspaceError(
                "legacy shared_capacity and shared_model_capacity_bindings are mutually exclusive")
        model_bindings: list[Mapping[str, str]] = []
        seen_model_accounts: set[tuple[str, str]] = set()
        for item in model_capacity:
            fields = {"database", "policy_ref", "policy_hash", "provider",
                      "credential_slot_ref", "scope_ref", "account_ref"}
            if not isinstance(item, Mapping) or set(item) != fields:
                raise WorkspaceError(
                    "shared model capacity binding has an invalid closed shape")
            database = _absolute(item["database"],
                                 "shared_model_capacity_bindings.database")
            if database.parent != (host_root / "fleet-capacity").resolve():
                raise WorkspaceError(
                    "shared model capacity database must be directly under host fleet-capacity")
            for key in fields - {"database", "policy_hash", "provider"}:
                if not isinstance(item[key], str) or _REF.fullmatch(item[key]) is None:
                    raise WorkspaceError(f"shared model capacity {key} is invalid")
            if (not isinstance(item["policy_hash"], str)
                    or _SHA.fullmatch(item["policy_hash"]) is None):
                raise WorkspaceError("shared model capacity policy hash is invalid")
            if (not isinstance(item["provider"], str) or not item["provider"]
                    or any(char.isspace() for char in item["provider"])):
                raise WorkspaceError("shared model capacity provider is invalid")
            identity = (item["provider"], item["credential_slot_ref"])
            if identity in seen_model_accounts:
                raise WorkspaceError(
                    "shared model capacity provider accounts must be unique")
            seen_model_accounts.add(identity)
            model_bindings.append(dict(item))
        connector_capacity = value.get("shared_connector_capacity", [])
        if not isinstance(connector_capacity, list):
            raise WorkspaceError("shared_connector_capacity must be an array")
        connector_bindings: list[Mapping[str, str]] = []
        seen_bindings: set[tuple[str, str]] = set()
        for item in connector_capacity:
            if not isinstance(item, Mapping) or set(item) != {
                    "database", "policy_ref", "policy_hash"}:
                raise WorkspaceError("shared connector capacity binding has an invalid closed shape")
            database = _absolute(item["database"], "shared_connector_capacity.database")
            if database.parent != (host_root / "fleet-capacity").resolve():
                raise WorkspaceError(
                    "shared connector capacity database must be directly under host fleet-capacity")
            if (not isinstance(item["policy_ref"], str) or ":" not in item["policy_ref"]
                    or not isinstance(item["policy_hash"], str)
                    or _SHA.fullmatch(item["policy_hash"]) is None):
                raise WorkspaceError("shared connector capacity policy binding is invalid")
            identity_key = (item["policy_ref"], item["policy_hash"])
            if identity_key in seen_bindings:
                raise WorkspaceError("shared connector capacity bindings must be unique")
            seen_bindings.add(identity_key)
            connector_bindings.append(dict(item))
        manifest = None if manifest_path is None else _absolute(str(manifest_path), "manifest_path")
        if manifest is not None:
            _inside(manifest, root, "manifest_path")
            if manifest != (root / "workspace.json").resolve():
                raise WorkspaceError("manifest_path is not workspace_root/workspace.json")
        return cls(workspace_id, slug, root, state, config, logs, spool, socket,
                   port, release_ref, release, shared_paths, capacity,
                   tuple(model_bindings),
                   tuple(connector_bindings),
                   value["content_hash"], manifest)

    def service_binding(self) -> dict[str, str]:
        if self.manifest_path is None:
            raise WorkspaceError("workspace manifest path is required for service binding")
        return {"workspace_id": self.workspace_id,
                "manifest_path": str(self.manifest_path),
                "manifest_hash": self.content_hash}


def load_workspace_manifest(path: str | Path) -> WorkspacePaths:
    manifest = Path(path).expanduser().resolve()
    try:
        raw = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise WorkspaceError("workspace manifest is unavailable or invalid") from exc
    return WorkspacePaths.from_manifest(raw, manifest_path=manifest)


def path_is_declared(path: str | Path, workspace: WorkspacePaths) -> bool:
    """Whether an absolute runtime path is workspace-owned or declared shared."""
    candidate = Path(path).expanduser().resolve()
    roots = (workspace.workspace_root, *workspace.shared_readonly_paths)
    return any(candidate == root or candidate.is_relative_to(root) for root in roots)


def validate_service_mapping_paths(
    value: Mapping[str, Any], workspace: WorkspacePaths
) -> None:
    """Check known path-bearing config fields without copying or exposing values."""
    suffixes = ("_path", "_db", "_socket", "_root", "_dir", "_executable")
    external_readonly_keys = {
        "broker_socket", "planner_broker_socket", "broker_auth_key",
        "planner_broker_auth_key", "openclaw_config_path", "tailscale_executable",
        "python_executable",
    }

    def visit(node: Any, key: str = "") -> None:
        if isinstance(node, Mapping):
            for child_key, child in node.items():
                if child_key == "workspace":
                    continue
                visit(child, str(child_key))
        elif isinstance(node, list):
            for child in node:
                visit(child, key)
        elif isinstance(node, str) and key.endswith(suffixes) and Path(node).is_absolute():
            candidate = Path(node).expanduser().resolve()
            if candidate == workspace.workspace_root or candidate.is_relative_to(
                    workspace.workspace_root):
                return
            if key not in external_readonly_keys or not any(
                    candidate == root or candidate.is_relative_to(root)
                    for root in workspace.shared_readonly_paths):
                raise WorkspaceError(
                    f"service path {key} is outside its permitted workspace boundary")

    visit(value)


def workspace_manifest(
    host_root: str | Path, slug: str, cockpit_port: int, release_ref: str,
    release_path: str | Path, *, workspace_id: str | None = None,
    shared_readonly_paths: Sequence[str | Path] = (),
    shared_capacity: Mapping[str, str] | None = None,
    shared_model_capacity_bindings: Sequence[Mapping[str, str]] = (),
    shared_connector_capacity: Sequence[Mapping[str, str]] = (),
) -> dict[str, Any]:
    if not isinstance(slug, str) or _SLUG.fullmatch(slug) is None:
        raise WorkspaceError("workspace slug is invalid")
    host = Path(host_root).expanduser().resolve()
    root = (host / "workspaces" / slug).resolve()
    release = Path(release_path).expanduser().resolve()
    shared = sorted({str(Path(item).expanduser().resolve())
                     for item in (*shared_readonly_paths, release)})
    identity = str(uuid.uuid4()) if workspace_id is None else workspace_id
    body: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION, "workspace_id": identity, "slug": slug,
        "workspace_root": str(root), "state_dir": str(root / "state" / "dalton-core"),
        "config_path": str(root / "config" / "service.json"),
        "log_dir": str(root / "logs"),
        "spool_dir": str(root / "state" / "dalton-core" / "connector-spool"),
        "writer_socket": str(root / "state" / "dalton-core" / "run" / "writer.sock"),
        "cockpit_port": cockpit_port, "release_ref": release_ref,
        "release_path": str(release), "shared_readonly_paths": shared,
    }
    if shared_capacity is not None:
        body["shared_capacity"] = dict(shared_capacity)
    if shared_model_capacity_bindings:
        body["shared_model_capacity_bindings"] = [
            dict(item) for item in shared_model_capacity_bindings]
    if shared_connector_capacity:
        body["shared_connector_capacity"] = [dict(item) for item in shared_connector_capacity]
    wire = {**body, "content_hash": content_hash(body)}
    WorkspacePaths.from_manifest(wire)
    return wire


def _existing_manifests(host_root: Path) -> Iterable[WorkspacePaths]:
    directory = host_root / "workspaces"
    if not directory.exists():
        return ()
    return tuple(load_workspace_manifest(path) for path in sorted(directory.glob("*/workspace.json")))


def validate_fleet_candidate(candidate: WorkspacePaths, host_root: str | Path) -> None:
    host = Path(host_root).expanduser().resolve()
    expected_parent = (host / "workspaces").resolve()
    if candidate.workspace_root.parent != expected_parent:
        raise WorkspaceError("workspace_root is outside the host workspaces directory")
    for current in _existing_manifests(host):
        same_manifest = current.manifest_path == candidate.manifest_path
        if same_manifest and current.content_hash == candidate.content_hash:
            continue
        if current.slug == candidate.slug or current.workspace_root == candidate.workspace_root:
            raise WorkspaceError("workspace slug or root is already registered")
        if current.workspace_id == candidate.workspace_id:
            raise WorkspaceError("workspace_id is already registered")
        if current.cockpit_port == candidate.cockpit_port:
            raise WorkspaceError("cockpit_port is already registered")


def create_workspace_manifest(
    host_root: str | Path, slug: str, cockpit_port: int, release_ref: str,
    release_path: str | Path, *, workspace_id: str | None = None,
    shared_readonly_paths: Sequence[str | Path] = (),
    shared_capacity: Mapping[str, str] | None = None,
    shared_model_capacity_bindings: Sequence[Mapping[str, str]] = (),
    shared_connector_capacity: Sequence[Mapping[str, str]] = (),
) -> WorkspacePaths:
    host = Path(host_root).expanduser().resolve()
    wire = workspace_manifest(host, slug, cockpit_port, release_ref, release_path,
                              workspace_id=workspace_id,
                              shared_readonly_paths=shared_readonly_paths,
                              shared_capacity=shared_capacity,
                              shared_model_capacity_bindings=shared_model_capacity_bindings,
                              shared_connector_capacity=shared_connector_capacity)
    host.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(host, 0o700)
    lock_path = host / ".workspace-registry.lock"
    lock_fd = os.open(lock_path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        os.chmod(lock_path, 0o600)
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        target = Path(wire["workspace_root"]) / "workspace.json"
        candidate = WorkspacePaths.from_manifest(wire, manifest_path=target)
        if target.exists():
            current = load_workspace_manifest(target)
            if current.content_hash == candidate.content_hash:
                return current
            raise WorkspaceError("workspace manifest already exists with different content")
        validate_fleet_candidate(candidate, host)
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=False)
        fd, temporary_name = tempfile.mkstemp(prefix=".workspace.", dir=target.parent)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(wire, stream, ensure_ascii=False, indent=2, sort_keys=True)
                stream.write("\n")
                stream.flush()
                os.fsync(stream.fileno())
            os.chmod(temporary_name, 0o600)
            os.replace(temporary_name, target)
        finally:
            Path(temporary_name).unlink(missing_ok=True)
    finally:
        os.close(lock_fd)
    return load_workspace_manifest(target)


def dry_run_legacy_migration(
    host_root: str | Path, legacy_state_dir: str | Path,
    legacy_config_path: str | Path, slug: str, cockpit_port: int,
    release_ref: str, release_path: str | Path,
) -> dict[str, Any]:
    state = Path(legacy_state_dir).expanduser().resolve()
    config = Path(legacy_config_path).expanduser().resolve()
    deterministic_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"dalton:{config}"))
    candidate = workspace_manifest(host_root, slug, cockpit_port, release_ref,
                                   release_path, workspace_id=deterministic_id)
    return {"mode": "dry_run", "writes_performed": False,
            "legacy": {"state_dir": str(state), "config_path": str(config)},
            "candidate": candidate,
            "actions": ["stop legacy services", "copy into a new workspace root",
                        "verify database and file hashes", "install namespaced services",
                        "retain legacy bytes until owner accepts migration"]}


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    create = sub.add_parser("create")
    create.add_argument("--host-root", type=Path, required=True)
    create.add_argument("--slug", required=True)
    create.add_argument("--cockpit-port", type=int, required=True)
    create.add_argument("--release-ref", required=True)
    create.add_argument("--release-path", type=Path, required=True)
    create.add_argument("--shared-readonly-path", type=Path, action="append", default=[])
    create.add_argument("--shared-capacity-database", type=Path)
    create.add_argument("--shared-capacity-policy-ref")
    create.add_argument("--shared-capacity-policy-hash")
    create.add_argument(
        "--shared-model-capacity-binding", type=Path, action="append", default=[],
        help="JSON file containing one exact provider/account capacity binding",
    )
    create.add_argument(
        "--shared-connector-capacity-binding", type=Path, action="append", default=[],
        help="JSON file containing one exact connector capacity policy binding",
    )
    show = sub.add_parser("show")
    show.add_argument("--manifest", type=Path, required=True)
    validate = sub.add_parser("validate")
    validate.add_argument("--manifest", type=Path, required=True)
    migrate = sub.add_parser("dry-run-migrate")
    migrate.add_argument("--host-root", type=Path, required=True)
    migrate.add_argument("--legacy-state-dir", type=Path, required=True)
    migrate.add_argument("--legacy-config", type=Path, required=True)
    migrate.add_argument("--slug", required=True)
    migrate.add_argument("--cockpit-port", type=int, required=True)
    migrate.add_argument("--release-ref", required=True)
    migrate.add_argument("--release-path", type=Path, required=True)
    args = parser.parse_args(argv)
    if args.command == "create":
        capacity_values = (args.shared_capacity_database,
                           args.shared_capacity_policy_ref,
                           args.shared_capacity_policy_hash)
        if any(value is not None for value in capacity_values) and not all(
                value is not None for value in capacity_values):
            parser.error("all shared-capacity fields must be supplied together")
        shared_capacity = None if not any(capacity_values) else {
            "database": str(args.shared_capacity_database.expanduser().resolve()),
            "policy_ref": args.shared_capacity_policy_ref,
            "policy_hash": args.shared_capacity_policy_hash,
        }
        model_bindings = []
        for path in args.shared_model_capacity_binding:
            try:
                binding = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                parser.error(f"invalid shared model capacity binding file: {exc}")
            model_bindings.append(binding)
        connector_bindings = []
        for path in args.shared_connector_capacity_binding:
            try:
                binding = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                parser.error(f"invalid shared connector capacity binding file: {exc}")
            connector_bindings.append(binding)
        result = create_workspace_manifest(args.host_root, args.slug,
                                           args.cockpit_port, args.release_ref,
                                           args.release_path,
                                           shared_readonly_paths=args.shared_readonly_path,
                                           shared_capacity=shared_capacity,
                                           shared_model_capacity_bindings=model_bindings,
                                           shared_connector_capacity=connector_bindings)
        wire: Any = {"manifest": str(result.manifest_path),
                     "workspace_id": result.workspace_id,
                     "content_hash": result.content_hash}
    elif args.command in {"show", "validate"}:
        result = load_workspace_manifest(args.manifest)
        wire = ({"status": "valid", "workspace_id": result.workspace_id,
                 "slug": result.slug, "content_hash": result.content_hash}
                if args.command == "validate" else
                json.loads(result.manifest_path.read_text(encoding="utf-8")))
    else:
        wire = dry_run_legacy_migration(
            args.host_root, args.legacy_state_dir, args.legacy_config, args.slug,
            args.cockpit_port, args.release_ref, args.release_path)
    print(json.dumps(wire, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
