"""Render scoped macOS LaunchAgents for Dalton writer and controller."""

from __future__ import annotations

import argparse
import os
import plistlib
import tempfile
from pathlib import Path
from typing import Any, Iterable

from .service import ServiceConfig


WRITER_LABEL = "space.lumos.dalton.writer"
# Operator-visible SEC User-Agent for lane runs (SEC fair-access policy asks
# for a contact string; no credentials are involved).
SEC_LANE_USER_AGENT = "Dalton Research Agent OS SEC company-facts lane (owner: lumos)"
# P13ak: SEC asks a client to say who it is and how to reach it. The statements
# lane says so in its own name rather than borrowing the facts lane's, and it
# carries the same contact address this Core already publishes on its outbound
# public requests -- the parser refuses an identity without one, which is how
# the first live tick failed.
STATEMENT_LANE_USER_AGENT = (
    "Dalton Research Agent OS SEC financial-statements lane everflow@lumos.space"
)
CONTROLLER_LABEL = "space.lumos.dalton.controller"
CONTROL_LABEL = "space.lumos.dalton.control"
THESIS_IMPACT_LABEL = "space.lumos.dalton.thesis-impact"


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
) -> dict[str, str]:
    destination = Path(launch_agents_dir).expanduser().resolve()
    bin_dir = Path(python_env_bin).expanduser().resolve()
    state = Path(state_dir).expanduser().resolve()
    config = Path(config_path).expanduser().resolve()
    logs = Path(log_dir).expanduser().resolve()
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
    # P13ak: the approved statements record, if this Core has one. Named by
    # version rather than discovered, so a future v3 is a deliberate edit here
    # and not something the writer picks up because a file appeared.
    statement_governance = (
        state / "connector-governance" / "sec-financial-statements-v2.json"
    )
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
    common: dict[str, Any] = {
        "RunAtLoad": True,
        "KeepAlive": True,
        "ProcessType": "Background",
        "ThrottleInterval": 10,
        "Umask": 0o077,
        "WorkingDirectory": str(state),
        "EnvironmentVariables": {"PYTHONUNBUFFERED": "1"},
    }
    writer = common | {
        "Label": WRITER_LABEL,
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
            str(state / "discovery-plans" / "us-it-services-alphaengine-v1.json"),
            # P9d-4a: web search discovery.  Same seed-once rule for the
            # proposed governance record and the hash-bound plan.  A networked
            # search needs the host broker below; without it the launcher
            # refuses before spawning and the tick reports the reason.  The
            # live mission also gates automation at the grant first.
            "--web-search-governance",
            str(state / "connector-governance" / "gemini-web-search-v1.json"),
            "--web-search-discovery-plan",
            str(state / "discovery-plans" / "us-it-services-web-search-v3.json"),
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
            str(state / "discovery-plans" / "us-it-services-sec-filings-v1.json"),
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
            # P13ak: the statements lane. Independent of the staging file above
            # -- it writes into the mission ledger, not the Cockpit inbox -- so
            # it is enabled by its own approved record being present, and stays
            # off on a Core that does not have one.
            [
                "--statement-lane-governance", str(statement_governance),
                "--statement-lane-user-agent", STATEMENT_LANE_USER_AGENT,
            ]
            if statement_governance.is_file() else []
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
        # P13o: the planner lane exists only when its configuration does, which
        # is only when the owner named a planner model at install time.
        planner_config = Path(state) / "research-planner-model-config.json"
        if planner_config.is_file():
            writer["ProgramArguments"].extend(
                ["--research-planner-model-config", str(planner_config)]
            )
        # P13ad: same rule for the deliverable's own drafting model -- present
        # only when the owner named one, and the screen falls back to the
        # extraction model otherwise.
        deliverable_config = Path(state) / "initial-screen-model-config.json"
        if deliverable_config.is_file():
            writer["ProgramArguments"].extend(
                ["--initial-screen-model-config", str(deliverable_config)]
            )
    controller = common | {
        "Label": CONTROLLER_LABEL,
        "ProgramArguments": [str(bin_dir / "daltond"), "--config", str(config)],
        "StandardOutPath": str(logs / "controller.stdout.log"),
        "StandardErrorPath": str(logs / "controller.stderr.log"),
    }
    writer_path = destination / f"{WRITER_LABEL}.plist"
    controller_path = destination / f"{CONTROLLER_LABEL}.plist"
    _atomic_plist(writer_path, writer)
    _atomic_plist(controller_path, controller)
    result = {"writer": str(writer_path), "controller": str(controller_path)}
    control_path = destination / f"{CONTROL_LABEL}.plist"
    if service_config is not None and service_config.control is not None:
        control = common | {
            "Label": CONTROL_LABEL,
            "ProgramArguments": [str(bin_dir / "dalton-control"), "--config", str(config)],
            "StandardOutPath": str(logs / "control.stdout.log"),
            "StandardErrorPath": str(logs / "control.stderr.log"),
        }
        _atomic_plist(control_path, control)
        result["control"] = str(control_path)
    elif control_path.exists():
        control_path.unlink()
    thesis_impact_path = destination / f"{THESIS_IMPACT_LABEL}.plist"
    if service_config is not None and service_config.thesis_impact is not None:
        thesis_impact = {
            key: value for key, value in common.items() if key != "KeepAlive"
        } | {
            "Label": THESIS_IMPACT_LABEL,
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
    )
    for name, path in paths.items():
        print(f"{name}={path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
