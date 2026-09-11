"""Render scoped macOS LaunchAgents for Dalton writer and controller."""

from __future__ import annotations

import argparse
import json
import os
import plistlib
import re
import tempfile
from pathlib import Path
from typing import Any, Iterable

from .lane_registry import LaunchAgentContext, lane_argv
from .service import ServiceConfig
from .mission_source_discovery import (ALPHAENGINE_SOURCE_REF, SEC_SOURCE_REF,
                                       WEB_SEARCH_SOURCE_REF, load_discovery_plan)
from .store import content_hash


WRITER_LABEL = "space.lumos.dalton.writer"
# Operator-visible SEC User-Agent for lane runs (SEC fair-access policy asks
# for a contact string; no credentials are involved).
SEC_LANE_USER_AGENT = "Dalton Research Agent OS SEC company-facts lane (owner: lumos)"
CONTROLLER_LABEL = "space.lumos.dalton.controller"
CONTROL_LABEL = "space.lumos.dalton.control"
THESIS_IMPACT_LABEL = "space.lumos.dalton.thesis-impact"
SEC_PLAN_SELECTOR = "sec-filings-plan-selection-v1.json"
WEB_PLAN_SELECTOR = "web-search-plan-selection-v1.json"
ALPHAENGINE_PLAN_SELECTOR = "alphaengine-plan-selection-v1.json"


def _selected_discovery_plan(
    state: Path, *, selector_filename: str, default_filename: str,
    source_ref: str, selector_schema: str, label: str,
) -> Path:
    """Resolve a selected plan without overwriting previous plan versions."""

    plans = (state / "discovery-plans").resolve()
    selector_path = plans / selector_filename
    default = plans / default_filename
    if not selector_path.exists() and not selector_path.is_symlink():
        return default
    try:
        selector = json.loads(selector_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} plan selector is unreadable") from exc
    expected_keys = {
        "schema_version", "id", "status", "source_ref", "plan_ref",
        "plan_hash", "plan_path", "content_hash",
    }
    if not isinstance(selector, dict) or set(selector) != expected_keys:
        raise ValueError(f"{label} plan selector has an invalid schema")
    body = {key: value for key, value in selector.items() if key != "content_hash"}
    if selector["content_hash"] != content_hash(body):
        raise ValueError(f"{label} plan selector content hash does not match")
    if (selector["schema_version"] != selector_schema
            or selector["status"] != "approved"
            or selector["source_ref"] != source_ref):
        raise ValueError(f"{label} plan selector is not an approved {label} selection")
    if not all(isinstance(selector[key], str) and selector[key] for key in (
            "id", "plan_ref", "plan_hash", "plan_path")):
        raise ValueError(f"{label} plan selector fields must be non-empty strings")
    relative = Path(selector["plan_path"])
    if relative.is_absolute() or len(relative.parts) != 1:
        raise ValueError(f"{label} selected plan must be a file in discovery-plans")
    selected = (plans / relative).resolve()
    if selected.parent != plans:
        raise ValueError(f"{label} selected plan escapes discovery-plans")
    plan = load_discovery_plan(selected)
    if (plan["source_ref"] != source_ref
            or plan["id"] != selector["plan_ref"]
            or plan["content_hash"] != selector["plan_hash"]):
        raise ValueError(f"{label} selected plan does not match its ref, hash, and source")
    return selected


def _sec_discovery_plan(state: Path) -> Path:
    return _selected_discovery_plan(
        state, selector_filename=SEC_PLAN_SELECTOR,
        default_filename="us-it-services-sec-filings-v1.json",
        source_ref=SEC_SOURCE_REF,
        selector_schema="sec-discovery-plan-selection-0.1", label="SEC")

def _alphaengine_discovery_plan(state: Path) -> Path:
    return _selected_discovery_plan(
        state, selector_filename=ALPHAENGINE_PLAN_SELECTOR,
        default_filename="us-it-services-alphaengine-v1.json",
        source_ref=ALPHAENGINE_SOURCE_REF,
        selector_schema="alphaengine-discovery-plan-selection-0.1", label="AlphaEngine")


def _web_discovery_plan(state: Path) -> Path:
    return _selected_discovery_plan(
        state, selector_filename=WEB_PLAN_SELECTOR,
        default_filename="us-it-services-web-search-v3.json",
        source_ref=WEB_SEARCH_SOURCE_REF,
        selector_schema="web-discovery-plan-selection-0.1", label="web")


def _atomic_plist(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temporary_name = tempfile.mkstemp(
        prefix=f".{path.name}.", suffix=".tmp", dir=path.parent
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            plistlib.dump(value, handle, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
    finally:
        if temporary.exists():
            temporary.unlink()


def render(
    launch_agents_dir: str | Path,
    python_env_bin: str | Path,
    state_dir: str | Path,
    config_path: str | Path,
    log_dir: str | Path,
    extraction_max_windows: int | None = None,
    extraction_numeric_windows: int | None = None,
    extraction_discovery_windows: int | None = None,
    alphaengine_owner_call_cap: int | None = None,
    label_namespace: str | None = None,
    workspace_manifest_path: str | Path | None = None,
) -> dict[str, str]:
    destination = Path(launch_agents_dir).expanduser().resolve()
    bin_dir = Path(python_env_bin).expanduser().resolve()
    state = Path(state_dir).expanduser().resolve()
    config = Path(config_path).expanduser().resolve()
    logs = Path(log_dir).expanduser().resolve()
    if label_namespace is None:
        labels = {
            "writer": WRITER_LABEL,
            "controller": CONTROLLER_LABEL,
            "control": CONTROL_LABEL,
            "thesis_impact": THESIS_IMPACT_LABEL,
        }
    else:
        if not re.fullmatch(r"space\.lumos\.dalton\.workspace\.[a-z0-9][a-z0-9-]{0,62}", label_namespace):
            raise ValueError("workspace LaunchAgent namespace is invalid")
        labels = {
            role: f"{label_namespace}.{role.replace('_', '-')}"
            for role in ("writer", "controller", "control", "thesis_impact")
        }
    sec_discovery_plan = _sec_discovery_plan(state)
    web_discovery_plan = _web_discovery_plan(state)
    logs.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(logs, 0o700)
    service_config = ServiceConfig.from_file(config) if config.is_file() else None
    # S7c-3: the writer's human-only transcript candidate ops stage into the
    # *same* CandidateStaging file the Cockpit reviews.  The only source of
    # truth for that path is control.config.research_review, so the writer
    # LaunchAgent derives it from there instead of a second path convention.
    # Without an embedded research_review block the writer runs without
    # staging and those ops answer ``rejected``.
    candidate_staging_path: str | None = None
    extraction_config_path: str | None = None
    selection_config_path: str | None = None
    if (
        service_config is not None
        and service_config.control is not None
        and service_config.control.research_review is not None
    ):
        candidate_staging_path = str(
            service_config.control.research_review.candidate_staging_path
        )
        if service_config.control.research_review.document_extraction_model_config_path is not None:
            extraction_config_path = str(service_config.control.research_review.document_extraction_model_config_path)
            selection_candidate = Path(extraction_config_path).with_name(
                "discovery-selection-model-config.json")
            if selection_candidate.is_file():
                selection_config_path = str(selection_candidate)
    # P10h: an explicit argument wins, but the config is what makes the setting
    # survive the next plain re-install.
    if extraction_max_windows is None and service_config is not None:
        extraction_max_windows = service_config.document_extraction_max_windows
    if extraction_numeric_windows is None and service_config is not None:
        extraction_numeric_windows = service_config.document_extraction_numeric_windows
    if extraction_discovery_windows is None and service_config is not None:
        extraction_discovery_windows = service_config.document_extraction_discovery_windows
    if alphaengine_owner_call_cap is None and service_config is not None:
        alphaengine_owner_call_cap = service_config.alphaengine_owner_call_cap
    # P9d-4d: both host brokers are OpenClaw plugin sockets in one state
    # directory, so the web search broker is derived from the planner's
    # configured broker path instead of a second convention.  Absent files
    # mean a networked search is refused before spawning.
    web_search_broker_socket: Path | None = None
    web_search_broker_auth_key: Path | None = None
    if (
        service_config is not None
        and service_config.bounded_planner is not None
        and service_config.bounded_planner.planner_broker_socket is not None
    ):
        broker_dir = Path(service_config.bounded_planner.planner_broker_socket).parent
        candidate_socket = broker_dir / "dalton-web-search-broker.sock"
        candidate_key = broker_dir / "dalton-web-search-broker.sock.key"
        if candidate_socket.exists() and candidate_key.exists():
            web_search_broker_socket = candidate_socket
            web_search_broker_auth_key = candidate_key
    environment = {"PYTHONUNBUFFERED": "1", "PYTHONDONTWRITEBYTECODE": "1"}
    if workspace_manifest_path is not None:
        manifest_path = Path(workspace_manifest_path).expanduser().resolve()
        from .workspace import load_workspace_manifest

        workspace = load_workspace_manifest(manifest_path)
        if workspace.state_dir != state or workspace.config_path != config:
            raise ValueError("workspace manifest does not bind the rendered state and config")
        environment["DALTON_WORKSPACE_MANIFEST"] = str(manifest_path)
    common: dict[str, Any] = {
        "RunAtLoad": True,
        "KeepAlive": True,
        "ProcessType": "Background",
        "ThrottleInterval": 10,
        "Umask": 0o077,
        "WorkingDirectory": str(state),
        "EnvironmentVariables": environment,
    }
    writer = common | {
        "Label": labels["writer"],
        # S7d: the writer hosts CPU-bound children (AlphaEngine acquisition,
        # SEC company-facts lane) that inherit its launchd process type.
        # Measured 2026-08-26: ``Background`` runs CPU work ~6x slower than
        # ``Standard`` and nothing inside the child can lift the clamp; a live
        # CTSH lane step took 6m31s under Background.  Only the writer moves
        # to ``Standard``; the other agents keep ``Background``.
        "ProcessType": "Standard",
        "ProgramArguments": [
            str(bin_dir / "dalton-writer"),
            "--db", str(state / "core.sqlite"),
            "--scheduler", str(state / "scheduler.sqlite"),
            "--socket", str(state / "run" / "writer.sock"),
            "--token-config", str(state / "writer-tokens.json"),
            "--transcript-spool-dir", str(state / "transcript-spool"),
            "--connector-governance",
            str(state / "connector-governance" / "alphaengine-get-document-v1.json"),
            # P9d-1: search-driven discovery.  The governance record is seeded
            # as *proposed* by install.sh; until the owner approves it in
            # place, launches are refused and the tick reports the reason.
            "--alphaengine-search-governance",
            str(state / "connector-governance" / "alphaengine-search-library-v1.json"),
            "--alphaengine-discovery-plan",
            str(_alphaengine_discovery_plan(state)),
            # P9d-4a: web search discovery.  Same seed-once rule for the
            # proposed governance record and the hash-bound plan.  A networked
            # search needs the host broker below; without it the launcher
            # refuses before spawning and the tick reports the reason.  The
            # live mission also gates automation at the grant first.
            "--web-search-governance",
            str(state / "connector-governance" / "gemini-web-search-v1.json"),
            "--web-search-discovery-plan",
            str(web_discovery_plan),
            # P9d-4b: public-web fetch of cited URLs.  Proposed record seeded
            # by install.sh; launches are refused until the owner approves it,
            # and nothing is queued until web search itself is connected.
            "--web-fetch-governance",
            str(state / "connector-governance" / "web-fetch-v1.json"),
            # P10u: the SEC filings index runs under the record the owner
            # signed; its plan is the 0.4 shape seeded by install.sh.
            "--sec-filings-governance",
            str(state / "connector-governance" / "sec-filings-index-v1.json"),
            "--sec-filings-discovery-plan",
            str(sec_discovery_plan),
        ] + (
            # P9d-4d: the host-owned web search broker is an OpenClaw plugin
            # socket in the same state directory as the model broker, so its
            # paths are derived from the planner's broker wiring rather than
            # configured twice.  They are passed only when both files exist;
            # otherwise a networked search is refused before spawning.
            [
                "--web-search-broker-socket", str(web_search_broker_socket),
                "--web-search-broker-auth-key", str(web_search_broker_auth_key),
                "--web-search-broker-client-id",
                service_config.bounded_planner.planner_broker_client_id,
            ]
            if web_search_broker_socket is not None else []
        ) + (
            [
                "--web-search-expected-provider",
                service_config.web_search_expected_provider,
            ]
            if (
                service_config is not None
                and service_config.web_search_expected_provider is not None
            ) else []
        ) + (
            # S7d: the SEC company-facts lane stages into the same Cockpit
            # staging file and is only enabled when that file is configured.
            [
                "--candidate-staging", candidate_staging_path,
                "--sec-lane-governance",
                # P9b-1: the company-facts template hash moved, so the lane runs
                # against the v2 record (approved in place by the owner).
                # P13z: it moved again -- the output contract now admits a
                # filing whose calendar frame passed to a later one -- so the
                # lane runs against v3, approved the same way.
                str(state / "connector-governance" / "sec-company-facts-v3.json"),
                "--sec-lane-user-agent", SEC_LANE_USER_AGENT,
            ]
            if candidate_staging_path is not None else []
        ) + (
            # P14-0: every registered lane says for itself, in its own module,
            # which arguments turn it on and what has to be on disk first.
            # This used to be one hand-written block per lane, in this order,
            # and the order is now the LaneSpec order.
            lane_argv(LaunchAgentContext(
                state=state,
                extraction_model_config_path=extraction_config_path,
                candidate_staging_path=candidate_staging_path,
            ))
        ) + (
            # P8c-4c: the bounded planner's model call runs inside the writer
            # (it accounts into this Core); derive its broker wiring from the
            # driver's service block so there is one source of truth.
            [
                "--planner-routing-policy",
                service_config.bounded_planner.planner_routing_policy_ref,
                "--planner-credential-slots",
                ",".join(service_config.bounded_planner.planner_credential_slot_refs),
                "--planner-model-router-db",
                str(service_config.bounded_planner.planner_model_router_db),
                "--planner-broker-socket",
                str(service_config.bounded_planner.planner_broker_socket),
                "--planner-broker-auth-key",
                str(service_config.bounded_planner.planner_broker_auth_key),
                "--planner-broker-client-id",
                service_config.bounded_planner.planner_broker_client_id,
                "--planner-expected-agent-id",
                service_config.bounded_planner.planner_expected_agent_id,
            ]
            if (
                service_config is not None
                and service_config.bounded_planner is not None
                and service_config.bounded_planner.planner_routing_policy_ref
                is not None
            )
            else []
        ),
        "StandardOutPath": str(logs / "writer.stdout.log"),
        "StandardErrorPath": str(logs / "writer.stderr.log"),
    }
    if extraction_config_path is not None:
        writer["ProgramArguments"].extend(["--document-extraction-model-config", extraction_config_path])
        # P10f: reading throughput is windows-per-tick times ticks-per-hour.
        # Left unset the writer keeps its own default; every window is a paid
        # model call, so this rises with the mission budget, not on its own.
        if extraction_max_windows is not None:
            writer["ProgramArguments"].extend(
                ["--document-extraction-max-windows", str(int(extraction_max_windows))]
            )
        if extraction_numeric_windows is not None:
            writer["ProgramArguments"].extend(
                ["--document-extraction-numeric-windows",
                 str(int(extraction_numeric_windows))]
            )
        if extraction_discovery_windows is not None:
            writer["ProgramArguments"].extend(
                ["--document-extraction-discovery-windows",
                 str(int(extraction_discovery_windows))]
            )
        if alphaengine_owner_call_cap is not None:
            writer["ProgramArguments"].extend(
                ["--alphaengine-owner-call-cap", str(int(alphaengine_owner_call_cap))]
            )
    if selection_config_path is not None:
        writer["ProgramArguments"].extend(
            ["--discovery-selection-model-config", selection_config_path]
        )
    controller = common | {
        "Label": labels["controller"],
        "ProgramArguments": [str(bin_dir / "daltond"), "--config", str(config)],
        "StandardOutPath": str(logs / "controller.stdout.log"),
        "StandardErrorPath": str(logs / "controller.stderr.log"),
    }
    writer_path = destination / f"{labels['writer']}.plist"
    controller_path = destination / f"{labels['controller']}.plist"
    _atomic_plist(writer_path, writer)
    _atomic_plist(controller_path, controller)
    result = {"writer": str(writer_path), "controller": str(controller_path)}
    control_path = destination / f"{labels['control']}.plist"
    if service_config is not None and service_config.control is not None:
        control = common | {
            "Label": labels["control"],
            "ProgramArguments": [str(bin_dir / "dalton-control"), "--config", str(config)],
            "StandardOutPath": str(logs / "control.stdout.log"),
            "StandardErrorPath": str(logs / "control.stderr.log"),
        }
        _atomic_plist(control_path, control)
        result["control"] = str(control_path)
    elif control_path.exists():
        control_path.unlink()
    thesis_impact_path = destination / f"{labels['thesis_impact']}.plist"
    if service_config is not None and service_config.thesis_impact is not None:
        thesis_impact = {
            key: value for key, value in common.items() if key != "KeepAlive"
        } | {
            "Label": labels["thesis_impact"],
            "StartInterval": int(service_config.thesis_impact_interval_seconds or 300),
            "ProgramArguments": [
                str(bin_dir / "dalton-thesis-impact"),
                "--config",
                str(config),
            ],
            "StandardOutPath": str(logs / "thesis-impact.stdout.log"),
            "StandardErrorPath": str(logs / "thesis-impact.stderr.log"),
        }
        _atomic_plist(thesis_impact_path, thesis_impact)
        result["thesis_impact"] = str(thesis_impact_path)
    elif thesis_impact_path.exists():
        thesis_impact_path.unlink()
    return result


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Render Dalton macOS LaunchAgents")
    parser.add_argument("--launch-agents-dir", type=Path, required=True)
    parser.add_argument("--python-env-bin", type=Path, required=True)
    parser.add_argument("--state-dir", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--log-dir", type=Path, required=True)
    parser.add_argument("--label-namespace")
    parser.add_argument("--workspace-manifest", type=Path)
    parser.add_argument(
        "--extraction-numeric-windows", type=int, default=None,
        help="Windows per tick also read for figures (0..50); omit to keep the config value",
    )
    parser.add_argument(
        "--extraction-discovery-windows", type=int, default=None,
        help="Windows per tick also read for what the market watches (0..50); "
             "omit to keep the config value",
    )
    parser.add_argument(
        "--extraction-max-windows", type=int, default=None,
        help="Extraction windows per controller tick (1..50); omit to keep the writer default",
    )
    args = parser.parse_args(list(argv) if argv is not None else None)
    if args.extraction_max_windows is not None and not 1 <= args.extraction_max_windows <= 50:
        raise SystemExit("--extraction-max-windows must be 1..50")
    if args.extraction_numeric_windows is not None and not 0 <= args.extraction_numeric_windows <= 50:
        raise SystemExit("--extraction-numeric-windows must be 0..50")
    if args.extraction_discovery_windows is not None and not 0 <= args.extraction_discovery_windows <= 50:
        raise SystemExit("--extraction-discovery-windows must be 0..50")
    paths = render(
        args.launch_agents_dir,
        args.python_env_bin,
        args.state_dir,
        args.config,
        args.log_dir,
        extraction_max_windows=args.extraction_max_windows,
        extraction_numeric_windows=args.extraction_numeric_windows,
        extraction_discovery_windows=args.extraction_discovery_windows,
        label_namespace=args.label_namespace,
        workspace_manifest_path=args.workspace_manifest,
    )
    for name, path in paths.items():
        print(f"{name}={path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
