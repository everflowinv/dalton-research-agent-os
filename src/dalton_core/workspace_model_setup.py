"""Portable, call-free model runtime setup for an isolated workspace.

The template contains declarations, never router decisions, budget usage or
broker key bytes.  Installation recreates those declarations through their
normal registries and rewrites every lane configuration to workspace-local
authorities while retaining exact host broker path references.
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
from pathlib import Path
from typing import Any, Mapping

from .document_extraction import validate_model_config
from .model_router import ModelRouter
from .store import canonical_json, content_hash
from .thesis_impact_budget import ThesisImpactBudgetStore
from .workspace import WorkspacePaths, load_workspace_manifest

SCHEMA_VERSION = "workspace-model-runtime-template-0.1"
KIND = "dalton-workspace-model-runtime"
EXPECTED_CONFIG_COUNT = 21
EXPECTED_CONFIG_NAMES = frozenset({
    "claim-index-model-config.json", "company-dossier-verifier-model-config.json",
    "discovery-selection-model-config.json", "document-extraction-model-config.json",
    "dossier-model-config.json", "earnings-season-model-config.json",
    "earnings-season-verifier-model-config.json", "event-judgement-model-config.json",
    "event-verifier-model-config.json", "initial-screen-model-config.json",
    "mission-document-draft-model-config.json", "mission-document-verifier-model-config.json",
    "registered-annual-report-draft-model-config.json",
    "registered-annual-report-verifier-model-config.json",
    "research-language-check-model-config.json", "research-language-revision-model-config.json",
    "research-localization-draft-model-config.json",
    "research-localization-verifier-model-config.json", "research-planner-model-config.json",
    "zero-base-review-model-config.json", "zero-base-review-verifier-model-config.json",
})
_FIELDS = {"schema_version", "kind", "source", "broker", "configs",
           "profiles", "policies", "budget_policies",
           "shared_call_budget_policy_path", "shared_readonly_paths", "content_hash"}
_LOCAL_ROUTER = "model-router.sqlite"
_LOCAL_BUDGET = "thesis-impact-budget.sqlite"


class WorkspaceModelSetupError(RuntimeError):
    pass


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise WorkspaceModelSetupError(f"invalid JSON: {path.name}") from exc
    if not isinstance(value, dict):
        raise WorkspaceModelSetupError(f"JSON object required: {path.name}")
    return value


def _owner_write(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    tmp = path.with_name(f".{path.name}.tmp")
    tmp.write_text(canonical_json(value) + "\n", encoding="utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, path)


def _rows_by_refs(connection: sqlite3.Connection, table: str, json_column: str,
                  ref_column: str, refs: set[str]) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    pending = set(refs)
    seen: set[str] = set()
    while pending:
        ref = pending.pop()
        if ref in seen:
            continue
        row = connection.execute(
            f"SELECT {json_column} FROM {table} WHERE {ref_column}=?", (ref,)
        ).fetchone()
        if row is None:
            raise WorkspaceModelSetupError(f"runtime declaration is missing: {ref}")
        wire = json.loads(row[0])
        seen.add(ref)
        prior = wire.get("prior_version_ref")
        if prior:
            pending.add(prior)
        result.append(wire)
    return sorted(result, key=lambda item: (item.get("id", ""), item["version"]))


def export_runtime_template(source_state: str | Path,
                            output_path: str | Path) -> dict[str, Any]:
    """Export only the immutable model declarations selected by a host."""
    state = Path(source_state).expanduser().resolve()
    files = sorted(state.glob("*model-config.json"))
    if ({f.name for f in files} != EXPECTED_CONFIG_NAMES
            or any(f.is_symlink() for f in files)):
        raise WorkspaceModelSetupError("source must contain the exact 21 model configs")
    configs = {f.name: validate_model_config(_read_json(f)) for f in files}
    router_paths = {Path(v["model_router_db"]).resolve() for v in configs.values()}
    budget_paths = {Path(v["budget_db"]).resolve() for v in configs.values()}
    sockets = {str(Path(v["broker_socket"]).resolve()) for v in configs.values()}
    keys = {str(Path(v["broker_auth_key"]).resolve()) for v in configs.values()}
    shared_policies = {v.get("shared_call_budget_policy_path") for v in configs.values()}
    if len(shared_policies) != 1:
        raise WorkspaceModelSetupError("source model configs do not share one call budget policy")
    shared_policy = next(iter(shared_policies))
    if shared_policy is not None:
        shared_policy = str(Path(shared_policy).resolve())
    if (any(Path(v["model_router_db"]).resolve().parent != state
            or Path(v["budget_db"]).resolve().parent != state for v in configs.values())):
        raise WorkspaceModelSetupError("source router and budget paths must be inside source state")
    if len(router_paths) != 1 or len(budget_paths) != 1 or len(sockets) != 1 or len(keys) != 1:
        raise WorkspaceModelSetupError("source model configs do not share one runtime")
    router_path, budget_path = next(iter(router_paths)), next(iter(budget_paths))
    if router_path.parent != state or budget_path.parent != state:
        raise WorkspaceModelSetupError("source router and budget paths must be inside source state")
    policy_refs = {v["routing_policy_ref"] for v in configs.values()}
    with ModelRouter(router_path, read_only=True) as router:
        policies = _rows_by_refs(router.connection, "model_routing_policy_versions",
                                 "policy_json", "policy_version_ref", policy_refs)
        profile_ids: set[str] = set()
        for policy in policies:
            profile_ids.update(policy["filters"]["allowed_profile_ids"])
            for override in (policy.get("purpose_overrides") or {}).values():
                if isinstance(override, Mapping):
                    profile_ids.update(override.get("profile_ids", ()))
        profile_refs: set[str] = set()
        for profile_id in profile_ids:
            row = router.connection.execute(
                "SELECT profile_version_ref FROM model_endpoint_profile_versions "
                "WHERE profile_id=? ORDER BY version DESC LIMIT 1", (profile_id,)
            ).fetchone()
            if row is None:
                raise WorkspaceModelSetupError(f"selected profile is missing: {profile_id}")
            profile_refs.add(row[0])
        profiles = _rows_by_refs(router.connection, "model_endpoint_profile_versions",
                                 "profile_json", "profile_version_ref", profile_refs)
    budget_refs = {v["budget_policy_ref"] for v in configs.values()}
    with ThesisImpactBudgetStore(budget_path, read_only=True) as budget:
        budget_policies: list[dict[str, Any]] = []
        pending = set(budget_refs)
        while pending:
            ref = pending.pop()
            row = budget.connection.execute(
                "SELECT record_json FROM thesis_impact_budget_policies WHERE policy_version_id=?",
                (ref,),
            ).fetchone()
            if row is None:
                raise WorkspaceModelSetupError(f"budget declaration is missing: {ref}")
            wire = json.loads(row[0])
            if any(x["policy_version_id"] == ref for x in budget_policies):
                continue
            budget_policies.append(wire)
            if wire.get("prior_version_id"):
                pending.add(wire["prior_version_id"])
        budget_policies.sort(key=lambda x: x["created_at"])
    templates = {}
    for name, config in configs.items():
        templates[name] = {k: v for k, v in config.items()
                           if k not in {"model_router_db", "budget_db"}}
        templates[name]["broker_socket"] = next(iter(sockets))
        templates[name]["broker_auth_key"] = next(iter(keys))
    body = {
        "schema_version": SCHEMA_VERSION, "kind": KIND,
        "source": {"config_count": len(templates)},
        "broker": {"socket_path": next(iter(sockets)), "auth_key_path": next(iter(keys)),
                   "sharing": "exact-shared-readonly-reference"},
        "configs": templates, "profiles": profiles, "policies": policies,
        "budget_policies": budget_policies,
        "shared_call_budget_policy_path": shared_policy,
        "shared_readonly_paths": [next(iter(sockets)), next(iter(keys))]
        + ([shared_policy] if shared_policy is not None else []),
    }
    bundle = {**body, "content_hash": content_hash(body)}
    _owner_write(Path(output_path).expanduser().resolve(), bundle)
    return bundle


def _validate_bundle(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != _FIELDS:
        raise WorkspaceModelSetupError("runtime template has an invalid closed shape")
    body = {k: value[k] for k in value if k != "content_hash"}
    if (value["schema_version"] != SCHEMA_VERSION or value["kind"] != KIND
            or value["content_hash"] != content_hash(body)):
        raise WorkspaceModelSetupError("runtime template identity or hash is invalid")
    configs = value["configs"]
    shared_policy = value["shared_call_budget_policy_path"]
    if shared_policy is not None and (not isinstance(shared_policy, str)
                                      or not Path(shared_policy).is_absolute()):
        raise WorkspaceModelSetupError("shared call budget policy path is invalid")
    if (not isinstance(configs, Mapping) or set(configs) != EXPECTED_CONFIG_NAMES
            or value["source"] != {"config_count": EXPECTED_CONFIG_COUNT}):
        raise WorkspaceModelSetupError("runtime template must contain the exact 21 configs")
    broker = value["broker"]
    if (not isinstance(broker, Mapping) or set(broker) != {"socket_path", "auth_key_path", "sharing"}
            or broker["sharing"] != "exact-shared-readonly-reference"):
        raise WorkspaceModelSetupError("runtime template broker binding is invalid")
    expected_shared = [broker["socket_path"], broker["auth_key_path"]]
    if shared_policy is not None:
        expected_shared.append(shared_policy)
    if value["shared_readonly_paths"] != expected_shared:
        raise WorkspaceModelSetupError("runtime template shared path closure is invalid")
    return dict(value)


def _require_local(path: Path, workspace: WorkspacePaths, name: str) -> None:
    if path.parent != workspace.state_dir:
        raise WorkspaceModelSetupError(f"{name} must be directly inside workspace state")


def _validate_config_paths(config: Mapping[str, Any], workspace: WorkspacePaths,
                           broker: Mapping[str, Any]) -> None:
    allowed_external = {broker["socket_path"], broker["auth_key_path"]}
    if config.get("shared_call_budget_policy_path"):
        allowed_external.add(config["shared_call_budget_policy_path"])

    def visit(value: Any) -> None:
        if isinstance(value, Mapping):
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)
        elif isinstance(value, str) and Path(value).is_absolute():
            resolved = Path(value).resolve()
            if (str(resolved) not in allowed_external
                    and not resolved.is_relative_to(workspace.state_dir)):
                raise WorkspaceModelSetupError("model config contains a cross-state path")

    visit(config)


def install_runtime_template(workspace_manifest: str | Path,
                             template_path: str | Path) -> dict[str, Any]:
    """Install a template without opening a broker or issuing a model call."""
    workspace = load_workspace_manifest(workspace_manifest)
    bundle = _validate_bundle(_read_json(Path(template_path).expanduser().resolve()))
    broker = bundle["broker"]
    shared_policy = bundle["shared_call_budget_policy_path"]
    shared_exact = {str(path) for path in workspace.shared_readonly_paths}
    required_shared = {broker["socket_path"], broker["auth_key_path"]}
    if shared_policy is not None:
        required_shared.add(shared_policy)
    if required_shared - shared_exact:
        raise WorkspaceModelSetupError(
            "broker socket and auth key must be exact shared_readonly_paths")
    router_path = (workspace.state_dir / _LOCAL_ROUTER).resolve()
    budget_path = (workspace.state_dir / _LOCAL_BUDGET).resolve()
    _require_local(router_path, workspace, "model router")
    _require_local(budget_path, workspace, "budget ledger")
    prepared: dict[str, tuple[Path, dict[str, Any]]] = {}
    for name, template in sorted(bundle["configs"].items()):
        if (not isinstance(name, str) or Path(name).name != name
                or not name.endswith("model-config.json")):
            raise WorkspaceModelSetupError("unsafe model config file name")
        config = validate_model_config({**template, "model_router_db": str(router_path),
                                        "budget_db": str(budget_path),
                                        **({"shared_call_budget_policy_path": shared_policy}
                                           if shared_policy is not None else {})})
        if (config["broker_socket"] != broker["socket_path"]
                or config["broker_auth_key"] != broker["auth_key_path"]):
            raise WorkspaceModelSetupError("config broker binding differs from template manifest")
        _validate_config_paths(config, workspace, broker)
        target = (workspace.state_dir / name).resolve()
        _require_local(target, workspace, "model config")
        prepared[name] = (target, config)
    workspace.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    with ModelRouter(router_path) as router:
        for profile in bundle["profiles"]:
            router.register_profile(profile)
        for policy in bundle["policies"]:
            router.register_policy(policy)
    with ThesisImpactBudgetStore(budget_path) as budget:
        for policy in bundle["budget_policies"]:
            budget.register_policy(
                policy_version_id=policy["policy_version_id"],
                day_cap_micros=policy["day_cap_micros"],
                prior_version_id=policy["prior_version_id"],
            )
    installed: dict[str, str] = {}
    for name, (target, config) in prepared.items():
        _owner_write(target, config)
        installed[name] = content_hash(config)
    return {"status": "installed", "workspace_id": workspace.workspace_id,
            "template_hash": bundle["content_hash"], "model_calls": 0,
            "router_db": str(router_path), "budget_db": str(budget_path),
            "configs": installed}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    export = commands.add_parser("export")
    export.add_argument("--source-state", type=Path, required=True)
    export.add_argument("--output", type=Path, required=True)
    install = commands.add_parser("install")
    install.add_argument("--workspace-manifest", type=Path, required=True)
    install.add_argument("--template", type=Path, required=True)
    args = parser.parse_args(argv)
    result = (export_runtime_template(args.source_state, args.output)
              if args.command == "export" else
              install_runtime_template(args.workspace_manifest, args.template))
    print(canonical_json(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
