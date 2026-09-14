"""Reuse host operating settings while keeping workspace research state empty."""

from __future__ import annotations

import argparse
import copy
import json
import os
from pathlib import Path
from typing import Any, Mapping

from .service import ServiceConfig
from .store import canonical_json, content_hash
from .workspace import load_workspace_manifest

SCHEMA_VERSION = "workspace-service-runtime-template-0.1"
KIND = "dalton-workspace-service-runtime"
_FIELDS = {"schema_version", "kind", "broker", "shared_readonly_paths",
           "operating", "content_hash"}


class WorkspaceServiceSetupError(RuntimeError):
    pass


def _read(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise WorkspaceServiceSetupError("service JSON is unavailable or invalid") from exc
    if not isinstance(value, dict):
        raise WorkspaceServiceSetupError("service JSON must be an object")
    return value


def _write(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(canonical_json(value) + "\n", encoding="utf-8")
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


def _seed(path: Path, value: Mapping[str, Any]) -> None:
    """Publish a generic runtime switch without replacing local authority."""
    if path.exists():
        if _read(path) != dict(value):
            raise WorkspaceServiceSetupError(f"workspace runtime switch differs: {path.name}")
        return
    _write(path, value)


def export_service_template(source_config: str | Path,
                            output_path: str | Path) -> dict[str, Any]:
    """Export model-engine settings, excluding subjects and destinations."""
    source = Path(source_config).expanduser().resolve()
    ServiceConfig.from_file(source)
    raw = _read(source)
    planner = copy.deepcopy(raw["bounded_planner"])
    thesis = copy.deepcopy(raw["thesis_impact"])
    if not planner.get("enabled"):
        raise WorkspaceServiceSetupError("source bounded planner engine must be enabled")
    for key in ("scheduler_db", "writer_socket", "token_config", "planner_model_router_db"):
        planner["config"].pop(key, None)
    for key in ("scheduler_db", "writer_socket", "token_config", "model_router_db", "budget_db"):
        thesis["config"].pop(key, None)
    thesis["config"]["company_thesis_refs"] = {}
    broker_values = {
        "socket_path": str(Path(planner["config"].pop("planner_broker_socket")).resolve()),
        "auth_key_path": str(Path(planner["config"].pop("planner_broker_auth_key")).resolve()),
        "openclaw_config_path": str(Path(
            raw["control"]["config"]["cockpit"]["openclaw_config_path"]
        ).resolve()),
    }
    thesis_socket = str(Path(thesis["config"].pop("broker_socket")).resolve())
    thesis_key = str(Path(thesis["config"].pop("broker_auth_key")).resolve())
    if (thesis_socket != broker_values["socket_path"]
            or thesis_key != broker_values["auth_key_path"]):
        raise WorkspaceServiceSetupError("source engines do not share one model broker")
    control_config = raw.get("control", {}).get("config", {})
    review = control_config.get("research_review")
    control_extensions: dict[str, Any] = {}
    if review is not None:
        control_extensions["research_review"] = {
            "reconcile_interval_seconds": review["reconcile_interval_seconds"],
        }
    intent = control_config.get("intent_composer")
    if intent is not None:
        intent = copy.deepcopy(intent)
        for key in ("staging_path", "scheduler_db", "model_router_db"):
            intent.pop(key, None)
        intent_socket = str(Path(intent.pop("broker_socket")).resolve())
        intent_key = str(Path(intent.pop("broker_auth_key")).resolve())
        if (intent_socket != broker_values["socket_path"]
                or intent_key != broker_values["auth_key_path"]):
            raise WorkspaceServiceSetupError("source control does not share one model broker")
        control_extensions["intent_composer"] = intent
    operating: dict[str, Any] = {
        "bounded_planner": planner,
        "thesis_impact": thesis,
        "document_extraction": copy.deepcopy(raw["document_extraction"]),
        "agenda": {"enabled": False,
                   "interval_seconds": raw.get("agenda", {}).get("interval_seconds", 60),
                   "config": {}},
        "weekly_brief": {"enabled": False,
                         "interval_seconds": raw.get("weekly_brief", {}).get("interval_seconds", 60),
                         "config": {}},
        "outbox": {"enabled": False,
                   "interval_seconds": raw.get("outbox", {}).get("interval_seconds", 60),
                   "config": {}},
        "backup": {k: copy.deepcopy(raw["backup"][k])
                   for k in raw["backup"] if k != "root"},
        "control_extensions": control_extensions,
    }
    for name in ("alphaengine_owner_call_cap", "web_search_expected_provider"):
        if name in raw:
            operating[name] = copy.deepcopy(raw[name])
    body = {"schema_version": SCHEMA_VERSION, "kind": KIND,
            "broker": {**broker_values, "sharing": "exact-shared-readonly-reference"},
            "shared_readonly_paths": [broker_values["socket_path"],
                                      broker_values["auth_key_path"],
                                      broker_values["openclaw_config_path"]],
            "operating": operating}
    result = {**body, "content_hash": content_hash(body)}
    rendered = canonical_json(result)
    source_state = str(Path(raw["core_db"]).resolve().parent)
    if source_state in rendered:
        raise WorkspaceServiceSetupError("template retained source state path")
    _write(Path(output_path).expanduser().resolve(), result)
    return result


def _validate_template(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != _FIELDS:
        raise WorkspaceServiceSetupError("service template has an invalid closed shape")
    body = {k: value[k] for k in value if k != "content_hash"}
    broker = value.get("broker")
    if (value.get("schema_version") != SCHEMA_VERSION or value.get("kind") != KIND
            or value.get("content_hash") != content_hash(body)):
        raise WorkspaceServiceSetupError("service template identity or hash is invalid")
    if (not isinstance(broker, Mapping)
            or set(broker) != {"socket_path", "auth_key_path", "openclaw_config_path", "sharing"}
            or broker["sharing"] != "exact-shared-readonly-reference"):
        raise WorkspaceServiceSetupError("service template broker binding is invalid")
    if value.get("shared_readonly_paths") != [broker["socket_path"], broker["auth_key_path"],
                                              broker["openclaw_config_path"]]:
        raise WorkspaceServiceSetupError("service template shared path closure is invalid")
    expected = {"bounded_planner", "thesis_impact", "document_extraction",
                "agenda", "weekly_brief", "outbox", "backup", "control_extensions"}
    operating = value.get("operating")
    if not isinstance(operating, Mapping) or set(operating) - {
            *expected, "alphaengine_owner_call_cap", "web_search_expected_provider"} or not expected.issubset(operating):
        raise WorkspaceServiceSetupError("service template operating sections are invalid")
    return dict(value)


def install_service_template(workspace_manifest: str | Path,
                             template_path: str | Path) -> dict[str, Any]:
    workspace = load_workspace_manifest(workspace_manifest)
    template = _validate_template(_read(Path(template_path).expanduser().resolve()))
    if not workspace.config_path.is_file():
        raise WorkspaceServiceSetupError("workspace must be bootstrapped before service setup")
    raw = _read(workspace.config_path)
    broker = template["broker"]
    if {Path(broker[name]).resolve() for name in (
            "socket_path", "auth_key_path", "openclaw_config_path")} - set(
            workspace.shared_readonly_paths):
        raise WorkspaceServiceSetupError("broker paths must be exact shared_readonly_paths")
    operating = copy.deepcopy(template["operating"])
    state = workspace.state_dir
    planner = operating["bounded_planner"]["config"]
    planner.update({
        "scheduler_db": str(state / "scheduler.sqlite"),
        "writer_socket": str(workspace.writer_socket),
        "token_config": str(state / "writer-tokens.json"),
        "planner_model_router_db": str(state / "model-router.sqlite"),
        "planner_broker_socket": broker["socket_path"],
        "planner_broker_auth_key": broker["auth_key_path"],
    })
    thesis = operating["thesis_impact"]["config"]
    thesis.update({
        "scheduler_db": str(state / "scheduler.sqlite"),
        "writer_socket": str(workspace.writer_socket),
        "token_config": str(state / "writer-tokens.json"),
        "model_router_db": str(state / "model-router.sqlite"),
        "budget_db": str(state / "thesis-impact-budget.sqlite"),
        "broker_socket": broker["socket_path"], "broker_auth_key": broker["auth_key_path"],
        "company_thesis_refs": {},
    })
    operating["backup"]["root"] = str(state / "backups")
    extensions = operating.pop("control_extensions")
    control = raw.get("control")
    if not isinstance(control, dict) or not isinstance(control.get("config"), dict):
        raise WorkspaceServiceSetupError("configure workspace control before service setup")
    if "research_review" in extensions:
        control["config"]["research_review"] = {
            "candidate_staging_path": str(state / "research-review" / "candidate-staging.sqlite"),
            "document_extraction_model_config_path": str(
                state / "document-extraction-model-config.json"),
            "reconcile_interval_seconds": extensions["research_review"][
                "reconcile_interval_seconds"],
            "transcript_review_directory": str(state / "research-review" / "inbox"),
        }
    if "intent_composer" in extensions:
        intent = copy.deepcopy(extensions["intent_composer"])
        intent.update({
            "staging_path": str(state / "intent" / "staging.sqlite"),
            "scheduler_db": str(state / "scheduler.sqlite"),
            "model_router_db": str(state / "model-router.sqlite"),
            "broker_socket": broker["socket_path"],
            "broker_auth_key": broker["auth_key_path"],
        })
        control["config"]["intent_composer"] = intent
    # Preserve bootstrap and runtime-setup ownership of base paths, plugins,
    # workspace identity and control. Replace only the exported engine fields.
    raw.update(operating)
    if raw.get("workspace") != workspace.service_binding():
        raise WorkspaceServiceSetupError("workspace service binding changed")
    try:
        ServiceConfig.from_mapping(raw)
        from .workspace import validate_service_mapping_paths
        validate_service_mapping_paths(raw, workspace)
    except Exception as exc:
        raise WorkspaceServiceSetupError("workspace service template is invalid") from exc
    _write(state / "model-catalog-sync.json", {
        "openclaw_config_path": broker["openclaw_config_path"],
        "model_router_db": str(state / "model-router.sqlite"),
    })
    # These switches contain no mission, company or source data.  Their
    # presence makes the admission-driven lanes available; each lane still
    # refuses work until Core contains a separately admitted research task.
    _seed(state / "mission-document-research-lane.json",
          {"schema_version": "0.1", "enabled": True})
    _seed(state / "mission-annual-research-lane.json",
          {"schema_version": "0.1", "enabled": True})
    _seed(state / "research-language-policy.json",
          {"schema_version": "research-language-policy:0.1", "required": True})
    _write(workspace.config_path, raw)
    try:
        # Rebind the Cockpit after research_review has supplied the extraction
        # fallback.  This is idempotent and also selects the installed planner
        # model configuration when present.
        from .cockpit_setup import install as install_cockpit
        install_cockpit(workspace.config_path)
        ServiceConfig.from_file(workspace.config_path)
    except Exception as exc:
        raise WorkspaceServiceSetupError("installed workspace service config is invalid") from exc
    return {"status": "installed", "workspace_id": workspace.workspace_id,
            "template_hash": template["content_hash"], "service_config": str(workspace.config_path),
            "research_state": "empty", "outbox": "disabled", "weekly_brief": "awaiting_mission"}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    export = commands.add_parser("export")
    export.add_argument("--source-config", type=Path, required=True)
    export.add_argument("--output", type=Path, required=True)
    install = commands.add_parser("install")
    install.add_argument("--workspace-manifest", type=Path, required=True)
    install.add_argument("--template", type=Path, required=True)
    args = parser.parse_args(argv)
    result = (export_service_template(args.source_config, args.output)
              if args.command == "export" else
              install_service_template(args.workspace_manifest, args.template))
    print(canonical_json(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
